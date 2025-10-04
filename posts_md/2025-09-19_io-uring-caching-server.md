---
Date: 2025-09-19
---

# io_uring caching server worklog

Thought I would try my hand at a "worklog" blog post.

My motivation is just to learn about io\_uring and similar technologies here. None of this code is used by my employer although it feasibly could be relevant. Mostly this was inspired by a former coworker from a couple of years ago [kixelated](https://github.com/kixelated?tab=repositories) who I'm sure is a far more talented engineer than I will ever be. Not an excuse to not learn of course.

If you're unfamiliar with [io_uring](https://man7.org/linux/man-pages/man7/io_uring.7.html): it's a Linux API for asynchronous I/O. The ideal is that you add requests to "do stuff" to a shared queue which the kernel reads from, the kernel then "does stuff", and once that stuff is done an event is placed on a different shared queue for you to know the result of the operation.  The idea here is that if you can batch enough work to the submission queue then you can avoid many costly syscalls.

This is in contrast to something like epoll where you tell the kernel to watch a bunch of file descriptors, tell you which ones are ready, and then you issue syscalls to read/write from each ready file descriptor.

Which of course was in contrast to spawning a thread or process per connection to watch a single file descriptor.

## Summary of where I'm at

This post is intended to be a stream of consciousness for me to write in as I work through this problem. Here is a summary of where I'm at so far:

I have example servers written in tokio, glommio, and `io_uring` in [my bench repo](https://github.com/benhirsch24/iouring_bench) which serve a static asset (~1.3MB of text). Each of these are intentionally kept single-threaded for now. In this setup I can max out an EC2 instance c7g.8xlarge which has a 15Gbps NIC.

To add more syscalls (which should be where uring shines) I started to play around with sending the asset in chunks (eg: 4096 bytes instead of writing the whole thing at once). Tokio actually pulled ahead in terms of using less CPU.

My impression of the gap in performance is that my uring server basically busy-loops and calls submit too often. Ideally to have less CPU usage I'd be `submit_and_wait`ing to park the thread more often, but I can only really do that when there's no work to do. I haven't yet tested what that would look like.

I also played around with enabling SQPOLL for my uring server. This of course results in more CPU usage because it creates a kernel thread which busy-polls the submission queue. I could see this being useful in certain applications where you're generating a lot of work in some threads and delegate IO to a kernel thread.

If you happen to be reading this and have some suggestions feel free to email me!

These are the next things I'll look at:

* Add multi-shot accept
* Try higher batch sizes
* With higher batch sizes, examine how latencies shift
* Actually make it into a proxy server (maybe use nginx as the backend server)
* Make it into a file serving server (I hear tokio uses a thread pool for file reads while this wouldn't have to)

### My friend Chat G

I had a lot of chats with Mr C GPT to work through random things. I didn't want AI to figure it all out for me, but I do like to use it as a rubber-duck/interactive Google. Sometimes I felt like it wasn't at all helpful and we were going around in circles. I thought it would be fun to link some of these chats.

* [Talking about SQPOLL](https://chatgpt.com/share/68e14853-22fc-800a-a992-5e6a9f63bfdf)
* [Profiling](https://chatgpt.com/share/68e14863-dd7c-800a-abfc-6632e61b3d03)
* [Setting up a lima VM on my Mac](https://chatgpt.com/share/68e1486e-b7f8-800a-882b-dfe5b40e4103)
    * Also some other random and maybe embarassingly dumb questions
* [Submission queue depth](https://chatgpt.com/share/68e1489b-f890-800a-8e66-3dc3b5d3be02)

## Requirements

This is meant to be a fan-out caching server.

Routes:

* `/health`
* `/object/<object>`

HTTP/1.1 priorities:
1. Keepalive
2. Transfer-encoding: chunked at some point

Basic operation:
1. Request for object comes in
2. If it's in the cache, serve
3. If it's not in the cache but there's an outstanding request for it to the origin then subscribe the incoming request to that outgoing request
4. If there's no outstanding request to the origin then create the outstanding request

Good-to-haves:
1. TLS (try out kTLS)
2. TTLs for the cache or max objects in the cache

Goals:

* Get a working server
* Optimize it a bit
* Optimize it a bit more

## Worklog

### Setting up dev environment 9/19

```
limactl create --name=uring template://ubuntu-lts
```

Kept getting
```
Error: The repository 'http://ports.ubuntu.com/ubuntu-ports oracular Release' no longer has a Release file.
Notice: Updating from such a repository can't be done securely, and is therefore disabled by default.
Notice: See apt-secure(8) manpage for repository creation and user configuration details.
```

When setting it up the way ChatGPT wanted me to with

```
limactl create --name=uring --set='.images[0].location="https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-arm64.img"' ubuntu
```

Codex wasn't any better. Codex did helpfully get the rw mount set up.

Then ran a test which worked.

```
// save as uring-test.c
#include <liburing.h>
#include <stdio.h>

int main() {
    struct io_uring ring;
    if (io_uring_queue_init(8, &ring, 0) < 0) {
        perror("io_uring_queue_init");
        return 1;
    }
    printf("io_uring ready.\n");
    io_uring_queue_exit(&ring);
    return 0;
}
```

Then we set up rust with rustup

### First Uring Program 9/19

1. Create listening socket (port 80)
2. Submit an accept to kernel on submission queue
3. Wait for ready on completion queue
4. Exit

```
use io_uring::{IoUring, opcode, types};

use std::os::fd::AsRawFd;

fn main() {
    let mut uring = IoUring::new(32).expect("io_uring");

    let listener = std::net::TcpListener::bind("127.0.0.1:80").expect("tcp listener");
    let listener_fd = listener.as_raw_fd();
    let lfd = types::Fd(listener_fd);
    let mut sockaddr: libc::sockaddr = unsafe { std::mem::zeroed() };
    let mut addrlen: libc::socklen_t = std::mem::size_of::<libc::sockaddr>() as _;
    let accept_e = opcode::Accept::new(lfd, &mut sockaddr, &mut addrlen);
    unsafe {
        uring.submission().push(&accept_e.build().user_data(0x0e).into()).expect("first push");
    }

    loop {
        uring.submit_and_wait(1).expect("saw");
        break;
    }

    println!("Hello, world!");
}
```

Verify with `nc 127.0.0.1 80`

### Slightly more advanced: Read and Respond 9/20

[This is a super helpful (read: definitive) uring resource](https://unixism.net/loti/tutorial/webserver_liburing.html)

Also https://github.com/tokio-rs/io-uring/blob/master/io-uring-test/src/tests/net.rs

1. Create listening socket (port 80)
2. Submit an accept to kernel on submission queue
3. Wait for ready on completion queue
4. Submit read on submission queue
5. Read from completion queue
6. Write a response to submission queue and mark it "responded"
7. Once that returns then remove the request from the map and close the fd

The request parsing was quite annoying. I wanted to store the headers in the request object so that clients could send data gradually and it would keep parsing, but got caught up in borrow hell. After looking at the source https://docs.rs/httparse/latest/src/httparse/lib.rs.html#481 it seems like it re-parses each time anyways so that was pointless.

### Serve static blob 9/22

Easiest way to start testing the egress capabilities which is really what we're trying to push. Super easy, just do a refcount around a hashmap and a static loaded file into it.

### Early load tests

Decided to try out [vegeta](https://github.com/tsenart/vegeta)

Simple command to start:

```
echo "GET http://127.0.0.1:80/object/1" | ./vegeta -cpus 1 attack -duration=10s | tee results.bin | ./vegeta report
```

### Comparisons 9/27

As a brief aside, let's prototype some comparisons with glommio and then serve a static 1KB file with nginx.

Glommio is pretty straightforward. Obviously it's very nice that you can do async with it. You use their implementations of TcpStream and TcpListener which use io\_uring under the hood.

At some point I would want to reduce the number of allocations I do (always allocating 1KB request buffers on the heap). Re-use request objects or something.

First little bench, but of course this is on a VM locally.

Glommio:

```
ben@lima-uring:/Users/ben/iouring_cache/vegeta$ echo "GET http://127.0.0.1:80/object/1" | ./vegeta -cpus 1 attack -duration=10s | tee results.bin | ./vegeta report
Requests      [total, rate, throughput]         500, 50.10, 50.09
Duration      [total, attack, wait]             9.982s, 9.98s, 2.494ms
Latencies     [min, mean, 50, 90, 95, 99, max]  624.684µs, 3.317ms, 1.225ms, 3.398ms, 6.724ms, 57.548ms, 107.352ms
Bytes In      [total, mean]                     672646000, 1345292.00
Bytes Out     [total, mean]                     0, 0.00
Success       [ratio]                           100.00%
Status Codes  [code:count]                      200:500
Error Set:
```

My iouring:

```
Requests      [total, rate, throughput]         500, 50.10, 50.10
Duration      [total, attack, wait]             9.981s, 9.979s, 1.441ms
Latencies     [min, mean, 50, 90, 95, 99, max]  924.101µs, 1.522ms, 1.457ms, 2.015ms, 2.231ms, 2.796ms, 7.972ms
Bytes In      [total, mean]                     672646000, 1345292.00
Bytes Out     [total, mean]                     0, 0.00
Success       [ratio]                           100.00%
Status Codes  [code:count]                      200:500
Error Set:
```

First EC2 tests, I'm using a c7g.medium for my load test client which offers "up to 12.5Gbps". This means that you may be able to burst up to 12.5Gbps but realistically you'll get throttled if you go above for too long.

io\_uring:

```
[ssm-user@client ~]$ echo "GET http://172.31.16.182:8080/object/1" | ./vegeta -cpus 1 attack -duration=10s -rate=100/1s | tee results.bin | ./vegeta report
Requests      [total, rate, throughput]         1000, 100.10, 0.00
Duration      [total, attack, wait]             9.992s, 9.99s, 1.956ms
Latencies     [min, mean, 50, 90, 95, 99, max]  1.445ms, 2.142ms, 2.017ms, 2.697ms, 2.733ms, 2.785ms, 2.993ms
Bytes In      [total, mean]                     0, 0.00
Bytes Out     [total, mean]                     0, 0.00
Success       [ratio]                           0.00%
Status Codes  [code:count]                      0:1000
Error Set:
unexpected EOF
```

`[2025-09-28T05:09:00Z DEBUG iouring_cache] Responded! 5 331318`

Ok we're writing 330KB in the send call. I don't yet read the result of the write call so I'm probably filling up some buffer. I think probably the socket is giving backpressure once the TCP cwnd fills up which then blocks send.

Captured a peer connection with `ss -tinp state established`

```
0                     0                                    172.31.16.182:8080                                 172.31.23.85:33791
         cubic wscale:7,7 rto:200 rtt:0.255/0.127 mss:8949 pmtu:9001 rcvmss:536 advmss:8949 cwnd:10 segs_in:2 send 2807529412bps pacing_rate 5615058816bps delivered:1 app_limited rcv_space:56575 rcv_ssthresh:56575 minrtt:0.255 snd_wnd:62848
```

This doesn't really tell me too much. I'll just fix the sending code... another day. Tomorrow is my wedding anniversary, I shouldn't work on it tomorrow if I want to stay married.

ChatGPT was telling me to increase wmem (on server) and rmem (on load tester).

Initial wmem:

```
[ssm-user@server ~]$ cat /proc/sys/net/ipv4/tcp_wmem
4096    20480   4194304
```

Nah didn't work. Ok, this is pointless for me to dig into further just for fun. The issue is that the TCP window when the connection starts is fairly small so the buffer fills up when I write the full String of data to the socket.

Ugh I couldn't stay away. I was trying this on my local vm. If I set the send buffer to 3MB and I tune the wmem/rmem settings then I can get it to go all at once. Let's try this on EC2.

```
[2025-09-28T16:27:04Z DEBUG iouring_cache] Recv! flags: 0 result: 334006 ud: 5
[2025-09-28T16:27:04Z DEBUG iouring_cache] Responded! 5 334006
[2025-09-28T16:27:04Z DEBUG iouring_cache] Recv! flags: 0 result: 241623 ud: 5
[2025-09-28T16:27:04Z DEBUG iouring_cache] Responded! 5 241623
[2025-09-28T16:27:04Z DEBUG iouring_cache] Recv! flags: 0 result: 223725 ud: 5
[2025-09-28T16:27:04Z DEBUG iouring_cache] Responded! 5 223725
[2025-09-28T16:27:04Z DEBUG iouring_cache] Recv! flags: 0 result: 62643 ud: 5
[2025-09-28T16:27:04Z DEBUG iouring_cache] Responded! 5 62643
[2025-09-28T16:27:04Z DEBUG iouring_cache] Recv! flags: 0 result: 375858 ud: 5
[2025-09-28T16:27:04Z DEBUG iouring_cache] Responded! 5 375858
[2025-09-28T16:27:04Z DEBUG iouring_cache] Recv! flags: 0 result: 105633 ud: 5
[2025-09-28T16:27:04Z DEBUG iouring_cache] Responded! 5 105633
```
Nope, ok, let's stop. Also FYI: you can't strace io\_uring because of course the only syscall you'll see is `io_uring_enter` :')

If someone wants to inform me how to do this that would be great. Just for my own knowledge. Otherwise I'll have to dig through Linux source code or something.

### NEXT: Fix sending code 9/28

Trying to do this before my wife gets up :)

Wrote a very straightforward way of sending all the data but it looks like using a String is biting me in the butt when doing slicing with Unicode. I used ChatGPT (I know) to generate a bunch of text, but of course being AI, it added a bunch of Unicode llamas. So when it writes data but has to slice within a Unicode character the Rust String slicing code complains (as it should). I'd probably want to make this content-type application/bytes (which I should to send video anyways).

I ended up converting the String to a `bytes::Bytes` and slicing it like that which feels much more ergonomic and works.

*Interesting*: Normally for syscall errors the function returns -1 and the error code is appropriately set. For iouring instead the negated error code is returned. So for example, I've been getting an error -32. This is because I was curling the server with `curl -v address | head` so of course it only reads the first 10 lines then hangs up. Looked into it and 32 is the return code for `EPIPE` which makes sense - the other end hung up.

Of course if I had read the io uring man page I'd see this was called out.

#### Aside

For a while (eh 1-2 hours maybe?) I was testing the `/health` endpoint and slamming my head against the wall as to why the server was only sending 40 bytes. Why why why? Then I finally realized that was the health endpoint vegeta was testing. So here's some notes from a fool:

Hmm bytes in is 14,000? Only 20 bytes per request?

```
[2025-09-28T04:32:13Z DEBUG iouring_cache] Responded! 5 40
```

Oh yeah, only writing 40 bytes at a time or something crazy like that. Have to enqueue multiple writes (and I left a TODO there). Why would this be though? Let's investigate this for a while first before just fixing the issue. 40 bytes seems like a very small amount to write at a time... Also could this be something where I could use GSO?

ChatGPT suggested using `ss` to find tcp connection details, but the problem is that because I close the socket after the first write it's impossible to capture. Then I tried bpf programs tcptop and tcpconnect but didn't find them too useful.

Let's enable GSO and see what happens. Oh wait GSO was enabled already.

SHIT I was testing /health. Damn, wasted an hour working from 9-10pm while my wife was sleeping with the baby. I could have actually been productive lol. What happens when I do the right endpoint?

glommio:

```
[ssm-user@client ~]$ echo "GET http://172.31.16.182:8080/object/1" | ./vegeta -cpus 1 attack -duration=10s -rate=100/1s | tee results.bin | ./vegeta report
Requests      [total, rate, throughput]         1000, 100.10, 0.00
Duration      [total, attack, wait]             9.991s, 9.99s, 1.509ms
Latencies     [min, mean, 50, 90, 95, 99, max]  1.007ms, 1.653ms, 1.567ms, 2.243ms, 2.298ms, 2.347ms, 2.452ms
Bytes In      [total, mean]                     0, 0.00
Bytes Out     [total, mean]                     0, 0.00
Success       [ratio]                           0.00%
Status Codes  [code:count]                      0:1000
Error Set:
```

io\_uring:

```
Requests      [total, rate, throughput]         1000, 100.10, 0.00
Duration      [total, attack, wait]             9.992s, 9.99s, 1.956ms
Latencies     [min, mean, 50, 90, 95, 99, max]  1.445ms, 2.142ms, 2.017ms, 2.697ms, 2.733ms, 2.785ms, 2.993ms
Bytes In      [total, mean]                     0, 0.00
Bytes Out     [total, mean]                     0, 0.00
Success       [ratio]                           0.00%
Status Codes  [code:count]                      0:1000
Error Set:
unexpected EOF
```

Ok this looks much better. It's not the whole thing, but it's not 40 bytes. Going back to the previous section for this worklog.
`[2025-09-28T05:09:00Z DEBUG iouring_cache] Responded! 5 331318`

### Continuing to benchmark 9/29

Starting to benchmark more. Fixed the same negative return syscall problem.

At 100rps it's working well. That's doing ~1Gbps already.

Issues I hit:

**EBADF** - remove request and continue

**Panic on submission queue** - Started with queue depth of 32. Probably too small. I wonder what happens if we have more events than the queue depth? Should I submit them all multiple times? How do I measure the backlog of queue items and how do I know I need to shed load?

At 300rps for 60s that would be 1345292 bytes * 300 * 8 = ~3Gbps

We definitely started to exceed the bandwidth limitations:

```
[ssm-user@server ~]$ ethtool -S ens5 | grep exceed
     bw_in_allowance_exceeded: 0
     bw_out_allowance_exceeded: 2609
     pps_allowance_exceeded: 0
     conntrack_allowance_exceeded: 0
     linklocal_allowance_exceeded: 0
```

And on the client

```
[ssm-user@client ~]$ ethtool -S ens34 | grep exceed
     bw_in_allowance_exceeded: 11214
```

Ok it clearly cannot handle 300rps. It can't handle 200rps - we end up seeing 946 200s when we'd expect 12,000 for 60s. At 100rps - yes definitely, vegeta sees exactly 6000 requests and asset size * 6000 bytes. At 150rps we hit some level of collapse as well.

Error seems to be -104 which is a TCP reset. Something is rejecting the request somewhere in the stack. https://stackoverflow.com/questions/51137032/what-are-the-general-rules-for-getting-the-104-connection-reset-by-peer-error

I'm also running this on only one CPU on the client. The machine actually died on me, so I switched up to a c7g.8xl as well for the load tester.

150rps is good in that configuration. 250 too. So the limit of the c7g.8xl would be:

15Gbps = 15 * 2^30 * 8 bits per byte / 1345292 bytes in my asset = 1496 RPS

At 750rps it seemed fine. I had to turn off body capturing because it would fill the disk, I will have to revisit this. But all requests were successful.

At 1000rps for 10s we had one failure. Let's run for 60s. This suspiciously works... Should we just go for it? No, let's run for like 10 minutes and see the metrics appear on CloudWatch. `1000rps = 8*1345292 ~= 10Gbps`

And... that worked? That easy?

```
[ssm-user@ip-172-31-18-153 ~]$ echo "GET http://172.31.16.182:8080/object/1" | ./vegeta attack -duration=600s -rate=1000/1s -max-body=0 | tee results.bin | ./vegeta reportRequests      [total, rate, throughput]         600000, 1000.00, 1000.00Duration      [total, attack, wait]             10m0s, 10m0s, 1.943msLatencies     [min, mean, 50, 90, 95, 99, max]  1.196ms, 1.914ms, 1.873ms, 2.198ms, 2.341ms, 2.848ms, 232.304msBytes In      [total, mean]                     0, 0.00
Bytes Out     [total, mean]                     0, 0.00Success       [ratio]                           100.00%
Status Codes  [code:count]                      200:600000
Error Set:
```

I saw some bandwidth allowance exceeded on the ethtool stats, but otherwise very easy. `top` showed peak CPU on the server of ~30%.

Let's try 1400RPS which is roughly the limit of the instance.

Went up to ~65% CPU. We started getting lots of `epipe` errors, so probably the client hanging up?

```
   5921 ssm-user  20   0 5861732   5.6g   3344 R  64.5   9.0   4:10.30 iouring_cache
```

Btw enabled EC2 instance detailed metrics to get graphs. You plot NetworkIn/Out at the 1 minute resolution with the Sum metric and then do metric math of `(8 * m1) / PERIOD(m1)` to get Gbps

Yeah got lots and lots of errors and eventually the uring push failed again. But honestly considering the program is doing this with one thread that seems pretty good? Also I think I miscalculated bandwidth requirements and I exceeded the capabilities of the network limits for this instance size lol.

```
[ssm-user@server ~]$ ethtool -S ens5 | grep exceed
     bw_in_allowance_exceeded: 0
     bw_out_allowance_exceeded: 44654442
```

Let's back this off to 1250rps.

```
[ssm-user@ip-172-31-18-153 ~]$ echo "GET http://172.31.16.182:8080/object/1" | ./vegeta attack -duration=600s -rate=1250/1s -max-body=0 | tee results.bin | ./vegeta report
Requests      [total, rate, throughput]         750000, 1250.00, 1250.00
Duration      [total, attack, wait]             10m0s, 10m0s, 2.082ms
Latencies     [min, mean, 50, 90, 95, 99, max]  1.163ms, 1.97ms, 1.89ms, 2.249ms, 2.409ms, 2.957ms, 30s
Bytes In      [total, mean]                     0, 0.00
Bytes Out     [total, mean]                     0, 0.00
Success       [ratio]                           100.00%
Status Codes  [code:count]                      0:1  200:749999
Error Set:
Get "http://172.31.16.182:8080/object/1": context deadline exceeded (Client.Timeout exceeded while awaiting headers)
```

No problem. We hit ~13.5Gbps. CPU hit 3% max on the EC2 metrics, but I saw ~60% with top for the process (note there's 32 vCPUs). Next: 1350rps.

1350 seems to have maxed it out right at 14.7Gbps which is the instance limit. Pretty cool to see it done with 1 thread and with basically no errors. Curious to know how a more naive implementation would perform. `psrecord` says that we stayed right around 42 CPU% and ~25MB virtual memory.

![Cloudwatch graph of EC2 instance metrics](/images/1350rps-iouring-9-28.png "1350 RPS")

```
[ssm-user@ip-172-31-18-153 ~]$ echo "GET http://172.31.16.182:8080/object/1" | ./vegeta attack -duration=600s -rate=1350/1s -max-body=0 | tee results.bin | ./vegeta report
Requests      [total, rate, throughput]         810001, 1350.00, 1350.00
Duration      [total, attack, wait]             10m0s, 10m0s, 1.915ms
Latencies     [min, mean, 50, 90, 95, 99, max]  1.142ms, 1.946ms, 1.898ms, 2.254ms, 2.433ms, 3.073ms, 253.962ms
Bytes In      [total, mean]                     0, 0.00
Bytes Out     [total, mean]                     0, 0.00
Success       [ratio]                           100.00%
Status Codes  [code:count]                      200:810001
Error Set:
```

### More comparisons

I just want to make sure I understand **the value** of io\_uring in this context. Let's make a super basic equivalent with tokio (which has an epoll backend I believe) and one thread.

I used `tokio::runtime::Builder` to make a new current thread with `enable_io` and then I used a `tokio::task::LocalSet` to use `tokio::task::spawn_local` so I could continue using the `Rc<HashMap<...>>`. If I didn't do that then `tokio::spawn` would require the Future to be `Send` which would have meant doing `Arc<Mutex<HashMap<...>>>` which would introduce a lock, not the same thing as I have been doing.

Luckily after doing all that it just worked (tm) and I basically copied the glommio code I had written over. That's certainly the nice thing about having code that implements `AsyncReadExt / AsyncWriteExt` is that it's far more portable than the crud I've been writing to work with uring at a low level.

Let's test.

```
[ssm-user@ip-172-31-18-153 ~]$ echo "GET http://172.31.16.182:8080/object/1" | ./vegeta attack -duration=120s -rate=1350/1s -max-body=0 | tee results.bin | ./vegeta report
Requests      [total, rate, throughput]         162001, 1350.01, 1349.99
Duration      [total, attack, wait]             2m0s, 2m0s, 2.376ms
Latencies     [min, mean, 50, 90, 95, 99, max]  1.147ms, 2.004ms, 1.922ms, 2.402ms, 2.637ms, 3.358ms, 242.98ms
Bytes In      [total, mean]                     0, 0.00
Bytes Out     [total, mean]                     0, 0.00
Success       [ratio]                           100.00%
Status Codes  [code:count]                      200:162001
Error Set:
```

Tokio is honestly about the same. Slightly higher latencies across the board but by microseconds. Probably means that 15Gbps is just not that big of a deal AND that we're not doing enough syscalls...

## More challenges

What if we dramatically increase the number of syscalls? So for example in HLS Low Latency we're doing chunked-transfer encoding. Basically as each frame is produced by the transcoder it is writing that to the origin which writes it to any connections currently open for the object. Basically it's a big pub-sub system. So what if we wrote this file out in chunks? The situation I described of course has more of a timing element to it (each chunk of the video would be produced in 16ms intervals for 60fps).

I went with `2^14 = 16384` byte chunks which will be ~60 writes per serve.

### Tokio benchmarking

Amusingly that immediately redlined the tokio implementation at 1350rps whereas it used to be no problem. And the server died lol. Oh I think I wrote an infinite looping bug as it's redlining after one curl call.

Let's try that again.o

100rps is fine:

```
00:56:05     1001      3982    8.00    0.00    0.00    0.00    8.00    13  tokio
00:56:06     1001      3982    9.00    0.00    0.00    0.00    9.00    13  tokio
00:56:07     1001      3982    9.00    0.00    0.00    0.00    9.00    13  tokio
00:56:08     1001      3982    9.00    0.00    0.00    0.00    9.00    13  tokio
00:56:09     1001      3982    9.00    0.00    0.00    0.00    9.00    13  tokio
00:56:10     1001      3982    8.91    0.00    0.00    0.00    8.91    13  tokio
00:56:11     1001      3982    9.00    0.00    0.00    0.00    9.00    13  tokio
00:56:12     1001      3982    9.00    0.00    0.00    0.00    9.00    13  tokio
00:56:13     1001      3982    9.00    0.00    0.00    0.00    9.00    13  tokio
00:56:14     1001      3982    8.00    0.00    0.00    0.00    8.00    13  tokio
00:56:15     1001      3982    9.00    0.00    0.00    0.00    9.00    13  tokio
00:56:16     1001      3982    9.00    0.00    0.00    0.00    9.00    13  tokio
00:56:17     1001      3982    9.00    0.00    0.00    0.00    9.00    13  tokio
00:56:18     1001      3982    9.00    0.00    0.00    0.00    9.00    13  tokio
00:56:19     1001      3982    9.00    0.00    0.00    0.00    9.00    13  tokio
00:56:20     1001      3982    9.00    0.00    0.00    0.00    9.00    13  tokio
00:56:21     1001      3982    8.00    0.00    0.00    0.00    8.00    13  tokio
```

It's interesting that it's always on CPU 13. Does tokio pin it?

Fuck it, let's go to 1200rps:

```
00:57:05     1001      3982    0.00   76.00    0.00    0.00   76.00    18  tokio
00:57:06     1001      3982    0.00   73.00    0.00    0.00   73.00    18  tokio
00:57:07     1001      3982    0.00   74.00    0.00    0.00   74.00    18  tokio
00:57:08     1001      3982    0.00   74.00    0.00    1.00   74.00    18  tokio
00:57:09     1001      3982    0.00   75.00    0.00    0.00   75.00     9  tokio
00:57:10     1001      3982    0.00   75.00    0.00    0.00   75.00     9  tokio
00:57:11     1001      3982    0.00   75.00    0.00    0.00   75.00    28  tokio
00:57:12     1001      3982    0.00   75.00    0.00    0.00   75.00    28  tokio
00:57:13     1001      3982    0.00   77.00    0.00    1.00   77.00    18  tokio
00:57:14     1001      3982    0.00   74.00    0.00    0.00   74.00    18  tokio

00:57:14      UID       PID    %usr %system  %guest   %wait    %CPU   CPU  Command
00:57:15     1001      3982    0.00   74.00    0.00    0.00   74.00    18  tokio
00:57:16     1001      3982    0.00   73.00    0.00    0.00   73.00     9  tokio
00:57:17     1001      3982    0.00   74.00    0.00    0.00   74.00     5  tokio
00:57:18     1001      3982    0.00   74.00    0.00    1.00   74.00     5  tokio
00:57:19     1001      3982    0.00   73.00    0.00    0.00   73.00     5  tokio
```

Ok, CPU not pinned. Looks like we got close to maxing out there. 1300rps for some reason was actually much lower.

But after 3-4 minutes the whole thing died again lol.

```
[2025-09-30T01:03:13Z ERROR tokio] Did nto write all Broken pipe (os error 32)
[2025-09-30T01:03:13Z ERROR tokio] Did nto write all Broken pipe (os error 32)
```

## Uring benchmarking

Ok let's do the iouring at 300rps

```
00:44:53     1001      3543    6.00   26.00    0.00    1.00   32.00     1  iouring_cache
00:44:54     1001      3543    5.00   26.00    0.00    1.00   31.00    10  iouring_cache
00:44:55     1001      3543    6.00   26.00    0.00    0.00   32.00     7  iouring_cache
00:44:56     1001      3543    3.00   29.00    0.00    1.00   32.00     7  iouring_cache
00:44:57     1001      3543    4.00   28.00    0.00    0.00   32.00    18  iouring_cache
00:44:58     1001      3543    7.00   25.00    0.00    1.00   32.00    18  iouring_cache
00:44:59     1001      3543    8.00   26.00    0.00    0.00   34.00    18  iouring_cache
00:45:00     1001      3543    5.00   27.00    0.00    0.00   32.00    18  iouring_cache
00:45:01     1001      3543    8.00   25.00    0.00    0.00   33.00    18  iouring_cache
00:45:02     1001      3543    4.00   28.00    0.00    0.00   32.00    11  iouring_cache
00:45:03     1001      3543    8.00   24.00    0.00    0.00   32.00    26  iouring_cache
00:45:04     1001      3543    7.00   26.00    0.00    0.00   33.00    26  iouring_cache
00:45:05     1001      3543    9.00   23.00    0.00    1.00   32.00     8  iouring_cache
00:45:06     1001      3543    6.00   26.00    0.00    1.00   32.00     8  iouring_cache
00:45:07     1001      3543    8.00   23.00    0.00    0.00   31.00     7  iouring_cache
```

Ok, clearly more CPU usage nice. Let's try double, 600rps?

```
00:46:17     1001      3543    4.00   40.00    0.00    1.00   44.00    11  iouring_cache
00:46:18     1001      3543    6.00   37.00    0.00    0.00   43.00     5  iouring_cache
00:46:19     1001      3543    5.00   38.00    0.00    0.00   43.00     5  iouring_cache
00:46:20     1001      3543    5.00   39.00    0.00    0.00   44.00    18  iouring_cache
00:46:21     1001      3543    6.00   39.00    0.00    0.00   45.00    11  iouring_cache
00:46:22     1001      3543    4.00   41.00    0.00    0.00   45.00    11  iouring_cache
00:46:23     1001      3543    8.00   36.00    0.00    1.00   44.00    11  iouring_cache
00:46:24     1001      3543    6.00   39.00    0.00    0.00   45.00    11  iouring_cache
00:46:25     1001      3543    6.00   38.00    0.00    0.00   44.00    11  iouring_cache
00:46:26     1001      3543    7.00   37.00    0.00    0.00   44.00    18  iouring_cache
00:46:27     1001      3543    3.00   42.00    0.00    1.00   45.00    18  iouring_cache
00:46:28     1001      3543    6.00   37.00    0.00    0.00   43.00    18  iouring_cache
00:46:29     1001      3543    5.00   39.00    0.00    0.00   44.00    11  iouring_cache
00:46:30     1001      3543    5.00   39.00    0.00    0.00   44.00    11  iouring_cache
00:46:31     1001      3543    6.00   37.00    0.00    0.00   43.00    11  iouring_cache
00:46:32     1001      3543    7.00   37.00    0.00    1.00   44.00    18  iouring_cache
00:46:33     1001      3543    7.00   36.00    0.00    0.00   43.00     5  iouring_cache
00:46:34     1001      3543    7.00   36.00    0.00    0.00   43.00     5  iouring_cache
00:46:35     1001      3543    5.00   39.00    0.00    0.00   44.00     5  iouring_cache
00:46:36     1001      3543    3.00   43.00    0.00    0.00   46.00    18  iouring_cache
```

Hovering around 44%. There was a little bump to 60% at the start. Let's try 1000rps?

Yeah it's interesting, there's a little bump at the start then it settles back to ~50%. Dare I try 1300rps?

```
00:49:19     1001      3543   34.00   32.00    0.00    1.00   66.00    13  iouring_cache
00:49:20     1001      3543   25.00   42.00    0.00    0.00   67.00    13  iouring_cache
00:49:21     1001      3543   32.00   35.00    0.00    0.00   67.00    13  iouring_cache
00:49:22     1001      3543   44.00   23.00    0.00    1.00   67.00    13  iouring_cache
00:49:23     1001      3543   31.00   35.00    0.00    0.00   66.00    13  iouring_cache
00:49:24     1001      3543   33.00   34.00    0.00    0.00   67.00    26  iouring_cache
00:49:25     1001      3543   38.00   30.00    0.00    1.00   68.00    16  iouring_cache
00:49:26     1001      3543   36.00   32.00    0.00    0.00   68.00    16  iouring_cache
00:49:27     1001      3543   38.00   31.00    0.00    1.00   69.00    28  iouring_cache
00:49:28     1001      3543   44.00   26.00    0.00    1.00   70.00    28  iouring_cache
00:49:29     1001      3543   21.00   58.00    0.00    0.00   79.00    28  iouring_cache
00:49:30     1001      3543   18.00   59.00    0.00    1.00   77.00    11  iouring_cache
00:49:31     1001      3543   35.00   32.00    0.00    0.00   67.00    20  iouring_cache
00:49:32     1001      3543   40.00   27.00    0.00    0.00   67.00    20  iouring_cache

00:49:32      UID       PID    %usr %system  %guest   %wait    %CPU   CPU  Command
00:49:33     1001      3543   38.00   31.00    0.00    1.00   69.00     7  iouring_cache
00:49:34     1001      3543   43.00   25.00    0.00    0.00   68.00     7  iouring_cache
00:49:35     1001      3543   36.00   33.00    0.00    1.00   69.00     5  iouring_cache
```

No problem. Actually this has a higher CPU than tokio did!

Why? I looked at perf and saw that it's spending a lot of time in `submit_and_wait`. Partially this is because I'm calling it waiting for just 1 to return. ChatGPT recommended a little re-architecture:

1. Add events to the submission queue while there's capacity
2. If it meets some batch size (eg 64) then submit using the non-blocking submit
3. Read off the completion queue
4. Only if there's nothing going on do we `submit_and_wait`

Got this done. The next thing I looked at was adding some simple metrics. I also want to add a timeout event for metrics.

After adding a bunch of this stuff I definitely could use some abstraction around uring and such. Maybe another day I'll add some abstractions.

Hey that's better! For 1300rps:

```
04:37:37      UID       PID    %usr %system  %guest   %wait    %CPU   CPU  Command
04:37:38     1001      3542   43.00    2.00    0.00    0.00   45.00    17  iouring_cache
04:37:39     1001      3542   34.00   11.00    0.00    1.00   45.00    17  iouring_cache
04:37:40     1001      3542   30.00   17.00    0.00    0.00   47.00    17  iouring_cache
04:37:41     1001      3542   33.00   11.00    0.00    0.00   44.00    17  iouring_cache
04:37:42     1001      3542   34.00   11.00    0.00    0.00   45.00    17  iouring_cache
04:37:43     1001      3542   31.00   13.00    0.00    0.00   44.00    17  iouring_cache
04:37:44     1001      3542   27.00   18.00    0.00    0.00   45.00    17  iouring_cache
```

Cut it short:

```
[ssm-user@ip-172-31-18-153 ~]$ echo "GET http://172.31.16.182:8080/object/1" | ./vegeta attack -duration=360s -rate=1300/1s -max-body=0 | tee results.bin | ./vegeta report
^CRequests      [total, rate, throughput]         133056, 1300.02, 1299.99
Duration      [total, attack, wait]             1m42s, 1m42s, 1.702ms
Latencies     [min, mean, 50, 90, 95, 99, max]  1.164ms, 1.889ms, 1.854ms, 2.183ms, 2.338ms, 2.898ms, 21.862ms
Bytes In      [total, mean]                     0, 0.00
Bytes Out     [total, mean]                     0, 0.00
Success       [ratio]                           100.00%
Status Codes  [code:count]                      200:133056
Error Set:
```

Just double checking tokio at 1300rps:

```
04:40:28      UID       PID    %usr %system  %guest   %wait    %CPU   CPU  Command
04:40:29     1001      3656   15.00   31.00    0.00    0.00   46.00    27  tokio
04:40:30     1001      3656   11.00   36.00    0.00    0.00   47.00    27  tokio
04:40:31     1001      3656    8.00   39.00    0.00    0.00   47.00    27  tokio
04:40:32     1001      3656   11.00   35.00    0.00    0.00   46.00    27  tokio
04:40:33     1001      3656    9.00   36.00    0.00    0.00   45.00    27  tokio
04:40:34     1001      3656   11.00   35.00    0.00    0.00   46.00    27  tokio
04:40:35     1001      3656    8.00   37.00    0.00    1.00   45.00    27  tokio
04:40:36     1001      3656   10.00   38.00    0.00    0.00   48.00    27  tokio
04:40:37     1001      3656    9.00   36.00    0.00    0.00   45.00    27  tokio
```

Pretty comparable. It's interesting, tokio is clearly spending much more time in the %system while uring is doing more in usr. But the same amount of CPU (roughly) overall. I wonder what would happen if I change the chunk size? (I should parameterize this) Basically I'm wondering when uring would start to beat tokio.

### More syscalls by writing in chunks

Now, instead of writing the whole file (1.3M bytes) as quickly as possible by passing the whole buffer to `write` I'm writing it in chunks.

#### Chunk size 4096

Tokio, chunk size of 4096, 1300rps:

```
04:43:40      UID       PID    %usr %system  %guest   %wait    %CPU   CPU  Command
04:43:41     1001      3797   20.00   53.00    0.00    0.00   73.00    13  tokio
04:43:42     1001      3797   26.00   49.00    0.00    0.00   75.00    13  tokio
04:43:43     1001      3797   20.00   54.00    0.00    0.00   74.00    13  tokio
04:43:44     1001      3797   19.00   55.00    0.00    0.00   74.00    13  tokio
04:43:45     1001      3797   18.00   55.00    0.00    0.00   73.00    13  tokio
04:43:46     1001      3797   21.00   51.00    0.00    0.00   72.00    13  tokio
04:43:47     1001      3797   20.00   54.00    0.00    0.00   74.00    13  tokio
04:43:48     1001      3797   14.00   59.00    0.00    1.00   73.00    13  tokio
```

Uring, chunk size of 4096, 1300rps:

```
04:44:59     1001      3880   12.00   83.00    0.00    1.00   95.00     6  iouring_cache
04:45:00     1001      3880   16.00   84.00    0.00    0.00  100.00     9  iouring_cache
04:45:01     1001      3880   13.00   85.00    0.00    3.00   98.00    11  iouring_cache
04:45:02     1001      3880   14.00   79.00    0.00    5.00   93.00     5  iouring_cache
04:45:03     1001      3880   21.00   75.00    0.00    4.00   96.00     1  iouring_cache
04:45:04     1001      3880   15.00   84.00    0.00    1.00   99.00     1  iouring_cache
04:45:05     1001      3880   14.00   85.00    0.00    1.00   99.00    26  iouring_cache
04:45:06     1001      3880    8.00   88.00    0.00    4.00   96.00     7  iouring_cache
04:45:07     1001      3880   14.00   81.00    0.00    5.00   95.00     1  iouring_cache
04:45:08     1001      3880   11.00   88.00    0.00    2.00   99.00    23  iouring_cache
04:45:09     1001      3880   10.00   89.00    0.00    0.00   99.00     4  iouring_cache
04:45:10     1001      3880   19.00   79.00    0.00    2.00   98.00     9  iouring_cache
```

Oof blew that up. The backlog started growing:

```
[2025-09-30T04:46:34Z INFO  iouring_cache] Metrics: total_submitted=2199990 total_completed=2199983 backlog=4640
[2025-09-30T04:46:36Z INFO  iouring_cache] Metrics: total_submitted=2634166 total_completed=2634160 backlog=5802
[2025-09-30T04:46:44Z INFO  iouring_cache] Metrics: total_submitted=4752822 total_completed=4751793 backlog=9374
```

Also what's interesting is how because of that, as the application clears the backlog it tries to write to the socket but then of course the other end hung up so it gets EPIPE. Just something to note. What I'd look at next here to keep debugging:

* What role does the submission queue depth and the batch size play here?
* Add timings between connection accepted -> request read -> response written
* Do more active backlog management. Some ideas:
    * If the backlog grows more than N (size of uring?) then flip a bool to not enqueue accepts any more. Once it falls below threshold enqueue accepts
    * Could probably do something more clever with this by queueing `8 * (backlog / sqe capacity)` accepts or something

I also started looking at what `SQPOLL` would take. This [CloudFlare article](https://blog.cloudflare.com/missing-manuals-io_uring-worker-pool/) is pretty great. Also the [Lord of the Uring](https://unixism.net/loti/tutorial/sq_poll.html) one as well of course. Basically it would spawn a kernel thread (a thread that runs in kernel space) that busy polls the submission queue so the user application doesn't have to call `io_uring_enter`.

The downside is that if you don't submit enough work within some timeout that you define (say 2ms) then the kernel thread will exit and you need to re-start it by calling `io_uring_enter`. So there would be some active management logic to deal with that. It's something I want to play with in the future for sure.

I think the next thing I'd definitely want to do is do some code cleanup, abstraction, and parameterize so I can experiment quicker.

### 9/30 Refactoring

My goal with the refactoring is to abstract away the uring-bits from the request processing bits.

I don't want to make some super-generic routing server thing with middleware, this is intentionally low-level. But I think moving the iouring loop out of main and then having mostly just the Request processing be in main seems pretty reasonable.

Ok tbh refactoring is too hard when I have 30 minutes before my son's bath time and he woke me up 3 times last night so my abstract thinking is awful.

### 9/30 back to profiling

I was trying to dig more in with perf and flame graphs. Found this workflow:

On EC2

```
sudo perf record -F 199 -g --call-graph dwarf -p <pid> -- sleep 30
sudo perf script > out.data
s3 cp out.data s3://mybucket
```

On laptop:

Grab https://github.com/brendangregg/FlameGraph

```
s3 cp s3://mybucket/out.data .
./stackcollapse-perf.pl /Users/ben/iouring_cache/iouring_cache/out.perf > out.folded
./flamegraph.pl out.folded > flamegraph.svg
open flamegraph.svg
```

Looks like we're spending most of the time in `submit_and_wait`. But I thought the whole point is that we wouldn't do that too often? Maybe I can increase the batch size at the expense of more latency. Added a log to metric outputs and it shows the same thing.


#### 10/02 Quick aside: compare to glommio

Let's compare to glommio which is an io uring implementation (probably better than mine) and see if there's much difference

```
04:02:52     1001      3989   22.00   47.00    0.00    0.00   69.00     0  glommio
04:02:53     1001      3989   30.00   38.00    0.00    0.00   68.00     0  glommio
04:02:54     1001      3989   25.00   46.00    0.00    0.00   71.00     0  glommio
04:02:55     1001      3989   35.00   36.00    0.00    0.00   71.00     0  glommio
04:02:56     1001      3989   27.00   43.00    0.00    0.00   70.00     0  glommio
04:02:57     1001      3989   27.00   43.00    0.00    0.00   70.00     0  glommio
```

Actually not really.

Mine seems to push so much more into syscalls.

#### Going back

Tried using Claude to debug a bit and help.

I tried out SQPOLL which actually wasn't too hard to do.

```
05:59:34     1001      8021    9.00  144.00    0.00    0.00  153.00     5  iouring_cache
05:59:35     1001      8021   16.00  136.00    0.00    1.00  152.00     8  iouring_cache
05:59:36     1001      8021   13.00  139.00    0.00    0.00  152.00     8  iouring_cache
05:59:37     1001      8021   14.00  138.00    0.00    0.00  152.00     8  iouring_cache
05:59:38     1001      8021   10.00  144.00    0.00    1.00  154.00     5  iouring_cache
05:59:39     1001      8021    8.00  144.00    0.00    0.00  152.00     8  iouring_cache
05:59:40     1001      8021   12.00  142.00    0.00    1.00  154.00     5  iouring_cache
05:59:41     1001      8021    8.00  146.00    0.00    1.00  154.00     8  iouring_cache
05:59:42     1001      8021   11.00  140.00    0.00    0.00  151.00     8  iouring_cache
05:59:43     1001      8021   13.00  140.00    0.00    0.00  153.00     8  iouring_cache
```

### Wrapping this up

Ok I'm going to wrap this up for now and put it on the blog. The next things I'd do:

* Add multi-shot accept
* Try higher batch sizes
* Actually make it into a proxy server (maybe use nginx as the backend server)
* Make it into a file serving server (I hear tokio uses a thread pool for file reads while this wouldn't have to)
