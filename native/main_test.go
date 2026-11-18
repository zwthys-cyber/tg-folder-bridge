package main

import "testing"

func TestCoverageRejectsGapsAndAcceptsRetriedOverlappingRanges(t *testing.T){
 f:=&guardedFile{size:10,ranges:[]span{{0,4},{6,10}}}
 if f.covered(){t.Fatal("gap accepted")}
 f.ranges=append(f.ranges,span{3,7},span{0,4})
 if !f.covered(){t.Fatal("valid retried coverage rejected")}
}

func TestRequestLimits(t *testing.T){
 r:=request{APIID:1,APIHash:"01234567890123456789012345678901",Session:"private",UserID:1,ID:1,Reference:[]byte{1},DC:2,Size:1024,Path:"/private/spool/media.mp4",Workers:8,Pool:4,Reserve:10<<30,Timeout:1200}
 if !r.valid(){t.Fatal("valid request rejected")}
 r.Workers=32;if r.valid(){t.Fatal("unsafe concurrency accepted")}
 r.Workers=8;r.Size=5<<30;if r.valid(){t.Fatal("oversize accepted")}
 r.Size=1024;r.Reserve=0;if r.valid(){t.Fatal("unbounded disk accepted")}
}
