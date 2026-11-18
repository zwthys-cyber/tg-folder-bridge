// Media-only worker. Credentials enter through stdin and live only in memory.
// This process never sends messages, polls bot updates, or writes login sessions.
package main

import (
 "context"
 "crypto/sha256"
 "encoding/hex"
 "encoding/json"
 "errors"
 "io"
 "os"
 "os/signal"
 "path/filepath"
 "sort"
 "sync"
 "syscall"
 "time"

 "github.com/gotd/td/session"
 "github.com/gotd/td/telegram"
 "github.com/gotd/td/telegram/downloader"
 "github.com/gotd/td/tg"
 "github.com/gotd/td/tgerr"
)

type request struct {
 APIID int `json:"api_id"`
 APIHash string `json:"api_hash"`
 Session string `json:"session"`
 UserID int64 `json:"user_id"`
 ID int64 `json:"document_id"`
 AccessHash int64 `json:"access_hash"`
 Reference []byte `json:"file_reference"`
 DC int `json:"dc_id"`
 Size int64 `json:"size"`
 Path string `json:"path"`
 Workers int `json:"workers"`
 Pool int `json:"pool"`
 Reserve int64 `json:"reserve_bytes"`
 Verify bool `json:"verify"`
 Timeout int `json:"timeout_seconds"`
}

func (r request) valid() bool {
 return r.APIID>0 && len(r.APIHash)==32 && r.Session!="" && r.UserID>0 && r.ID!=0 &&
  len(r.Reference)>0 && r.DC>0 && r.Size>0 && r.Size<=4<<30 && filepath.IsAbs(r.Path) &&
  r.Workers>=1 && r.Workers<=8 && r.Pool>=1 && r.Pool<=4 && r.Reserve>=10<<30 &&
  r.Timeout>=30 && r.Timeout<=86400
}

type span struct{ start,end int64 }
type guardedFile struct {
 f *os.File
 size,reserve,total int64
 ranges []span
 seen map[span]bool
 mu sync.Mutex
}

func (f *guardedFile) WriteAt(p []byte,off int64) (int,error) {
 if off<0 || int64(len(p))>f.size-off {return 0,errors.New("range")}
 var disk syscall.Statfs_t
 if err:=syscall.Fstatfs(int(f.f.Fd()),&disk);err!=nil{return 0,err}
 if uint64(disk.Bavail)*uint64(disk.Bsize)<uint64(f.reserve)+uint64(len(p)){return 0,errors.New("reserve")}
 n,err:=f.f.WriteAt(p,off)
 f.mu.Lock();defer f.mu.Unlock()
 s:=span{off,off+int64(n)}
 if n>0 && !f.seen[s] {f.seen[s]=true;f.ranges=append(f.ranges,s);f.total+=int64(n)}
 return n,err
}

func (f *guardedFile) covered() bool {
 sort.Slice(f.ranges,func(i,j int)bool{return f.ranges[i].start<f.ranges[j].start})
 end:=int64(0)
 for _,s:=range f.ranges {if s.start>end{return false};if s.end>end{end=s.end}}
 return end==f.size
}

type protocol struct {mu sync.Mutex; encoder *json.Encoder}
func (p *protocol) emit(v any){p.mu.Lock();defer p.mu.Unlock();_ = p.encoder.Encode(v)}

func transfer(ctx context.Context,r request,p *protocol) error {
 auth,err:=session.TelethonSession(r.Session)
 if err!=nil{return errors.New("session")}
 memory:=&session.StorageMemory{}
 if err=(&session.Loader{Storage:memory}).Save(ctx,auth);err!=nil{return err}
 client:=telegram.NewClient(r.APIID,r.APIHash,telegram.Options{
  SessionStorage:memory,NoUpdates:true,AllowCDN:true,FilePoolSize:int64(r.Pool),
  DownloadScheduler:&downloader.SchedulerOptions{Min:1,Start:min(4,r.Workers),Max:r.Workers},
 })
 return client.Run(ctx,func(ctx context.Context)error{
  me,err:=client.Self(ctx);if err!=nil{return err}
  if me.Bot || me.ID!=r.UserID{return errors.New("identity")}
  invoker,err:=client.FileDC(ctx,r.DC);if err!=nil{return err}
  // Parent creates a private, bounded spool path; never persist session data.
  fd,err:=syscall.Open(r.Path,syscall.O_RDWR|syscall.O_TRUNC|syscall.O_NOFOLLOW,0600)
  if err!=nil{return err}
  file:=os.NewFile(uintptr(fd),r.Path);defer file.Close()
  if err=file.Truncate(r.Size);err!=nil{return err}
  writer:=&guardedFile{f:file,size:r.Size,reserve:r.Reserve,seen:map[span]bool{}}
  location:=&tg.InputDocumentFileLocation{ID:r.ID,AccessHash:r.AccessHash,FileReference:r.Reference}
  began,last:=time.Now(),time.Time{}
  _,err=client.DownloadVia(invoker,location).WithThreads(r.Workers).WithSize(r.Size).
   WithScheduler(client.DownloadScheduler(r.DC)).WithVerify(r.Verify).
   WithProgress(func(done,total int64){
    if time.Since(last)<time.Second && done<total{return};last=time.Now()
    snap:=client.DownloadScheduler(r.DC).Snapshot()
    p.emit(map[string]any{"event":"progress","bytes":done,"total":total,"seconds":time.Since(began).Seconds(),"window":snap.Window,"pool_limit":r.Pool})
   }).Parallel(ctx,writer)
  if err!=nil{return err}
  if !writer.covered(){return errors.New("coverage")}
  if err=file.Sync();err!=nil{return err}
  if _,err=file.Seek(0,0);err!=nil{return err}
  hash:=sha256.New();n,err:=io.Copy(hash,file);if err!=nil{return err}
  if n!=r.Size{return errors.New("size")}
  p.emit(map[string]any{"event":"complete","bytes":n,"total":r.Size,"sha256":hex.EncodeToString(hash.Sum(nil)),"seconds":time.Since(began).Seconds(),"pool_limit":r.Pool})
  return nil
 })
}

func main(){
 p:=&protocol{encoder:json.NewEncoder(os.Stdout)}
 var r request
 if err:=json.NewDecoder(io.LimitReader(os.Stdin,1<<20)).Decode(&r);err!=nil || !r.valid(){p.emit(map[string]any{"event":"error","code":"INVALID_REQUEST"});os.Exit(2)}
 ctx,cancel:=signal.NotifyContext(context.Background(),syscall.SIGTERM,syscall.SIGINT);defer cancel()
 ctx,timeout:=context.WithTimeout(ctx,time.Duration(r.Timeout)*time.Second);defer timeout()
 if err:=transfer(ctx,r,p);err!=nil{
  code,wait:="TRANSFER_FAILED",0
  for _,name:=range []string{"FLOOD_WAIT","FLOOD_PREMIUM_WAIT","FILE_REFERENCE_EXPIRED","AUTH_KEY_UNREGISTERED","AUTH_KEY_DUPLICATED"}{
   if rpc,ok:=tgerr.AsType(err,name);ok {code=name;wait=rpc.Argument;break}
  }
  if errors.Is(err,context.DeadlineExceeded){code="TIMEOUT"}
  // Do not print raw errors: request objects may contain credentials/references.
  p.emit(map[string]any{"event":"error","code":code,"wait_seconds":wait});os.Exit(1)
 }
}
