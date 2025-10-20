---
Date: 2025-10-19
---

# Uring worklog 3

At this point I have a working pubsub server built on io_uring with an async runtime which you can find as of [this commit](https://github.com/benhirsch24/iouring_bench/commit/17441ac4c35b6b65502581267f43e87082e71372).

This ended up being the way to go to write actual programs based on io_uring the easiest. At the end of the day it took me about one weekend (in between kid stuff, on-call work stuff, working out) to write a full async runtime (with the help of Claude for socratic support). I detail how I use AI in the worklog of course.

Next up I can finally get back to benchmarking. I definitely think I'm allocating too much and too often but of course, you have to let profiling guide you where to go.

## Table of Contents

[Callbacks](#callbacks) - Before I went down the async path I was trying to do everything with callbacks like Javascript used to do for async. This just got too unwieldy especially when you want to break things out into functions and pass around self. I eventually just abandoned it.

[Deep thoughts on coding new code](#deep-thoughts) - Some thoughts I jotted down about how to design new code modules. Note that I totally threw all this code away, I was trying to write BufferPools, I think because I was struggling with the self references moving into callbacks I described above. I wanted to have Buffers with lifetimes outside of the callbacks. Ultimately registering completion events to Futures is the easier way.

[Research on other frameworks](#research) - I did some research on other popular frameworks, reading through the documentation for Seastar (a C++ high performance framework) and the source code for Glommio and Tokio. This was helpful. Seastar does callbacks, but the type system for C++ is a little more permissive than Rust's.

[Going async](#async) - Here's the meat of where I went async. I didn't really write too much about what I was struggling with because at this point I was talking to Claude ([conversation](https://claude.ai/share/ad2d1f01-d8d3-43c6-a0a6-26f9b91912e2)) to figure out my strategy. I found Claude to be very helpful in discussing the ideas and as a rubber-duck; often I'd first use it as a Google I can talk to, it would understand the problem but it's answer doesn't quite capture my full context, then by re-confirming its mistakes and my assumptions iteratively I can understand the problem space more. That was poorly phrased, read the conversation I think it's pretty straightforward.

I also listed the commits for the async work. I'd like to turn this itself into a blog post at some point.

Finally, back to benchmarking! Maybe in a couple weeks, I have a short week at work and then a wedding to attend.

## Worklog

### 10/13 - Spooky season {#callbacks}

13th day of October? We are officially in spooky season.

Removed the Publisher struct and now it's just callbacks. It's a little annoying to have to write it all in `main` to deal with the lifetimes of the Bytes/BytesMut buffers. If I put them in their own function then I run into the issue of the function returning (as it's just registering a callback) and then the memory doesn't live long enough, but that may be how I'm using it since the bytes should be heap allocated.  This is probably a good time to implement a global buffer pool.

Ah yep, lifetime is what it was.

See if you can spot the difference between what works and doesn't? This works:

```
fn publish(fd: RawFd) -> anyhow::Result<()> {
    let mut send_buffer = BytesMut::with_capacity(4096);
    send_buffer.put_slice(b"PUBLISH ch\r\n");
    let ptr = send_buffer.as_ptr();
    let to_send = send_buffer.len();
    debug!("Publisher sending");
    let send_e = opcode::Send::new(types::Fd(fd), ptr, to_send.try_into().unwrap());
    let ud = add_callback(move |res| {
        let _s = send_buffer;
        // TODO: What if send is bigger than res?
        info!("Publisher PUBLISH completed res={res}");
        let mut read_buffer = BytesMut::with_capacity(4096);
        let ptr = read_buffer.as_mut_ptr();
        let read_e = opcode::Recv::new(types::Fd(fd), ptr, read_buffer.capacity() as u32);
        let ud = add_callback(move |res: i32| {
            debug!("Publisher OK recv={res}");
            let newlen: usize = read_buffer.len() + (res as usize);
            unsafe {
                read_buffer.set_len(newlen)
            };
            let line = read_line(read_buffer).expect("There should be one read by now");
            if line != "OK\r\n" {
                anyhow::bail!("Expected OK");
            }
            debug!("Publisher received ok");

            let mut send_buffer = BytesMut::with_capacity(4096);
            send_buffer.put_slice(b"hello\r\n");
            let ptr = send_buffer.as_ptr();
            let to_send = send_buffer.len();
            let send_e = opcode::Send::new(types::Fd(fd), ptr, to_send.try_into().unwrap());
            let ud = add_callback(move |_res| {
                let _s = send_buffer;
                info!("Publisher sent message res={res}");
                Ok(())
            });
            uring::submit(send_e.build().user_data(ud.into()))?;
            Ok(())
        });
        let e = read_e.build().user_data(ud).into();
        uring::submit(e)?;
        Ok(())
    });
    uring::submit(send_e.build().user_data(ud))?;
    Ok(())
}
```

and this doesn't:

```
fn publish(fd: RawFd) -> anyhow::Result<()> {
    let mut send_buffer = BytesMut::with_capacity(4096);
    send_buffer.put_slice(b"PUBLISH ch\r\n");
    let ptr = send_buffer.as_ptr();
    let to_send = send_buffer.len();
    debug!("Publisher sending");
    let send_e = opcode::Send::new(types::Fd(fd), ptr, to_send.try_into().unwrap());
    let ud = add_callback(move |res| {
        // TODO: What if send is bigger than res?
        info!("Publisher PUBLISH completed res={res}");
        let mut read_buffer = BytesMut::with_capacity(4096);
        let ptr = read_buffer.as_mut_ptr();
        let read_e = opcode::Recv::new(types::Fd(fd), ptr, read_buffer.capacity() as u32);
        let ud = add_callback(move |res: i32| {
            debug!("Publisher OK recv={res}");
            let newlen: usize = read_buffer.len() + (res as usize);
            unsafe {
                read_buffer.set_len(newlen)
            };
            let line = read_line(read_buffer).expect("There should be one read by now");
            if line != "OK\r\n" {
                anyhow::bail!("Expected OK");
            }
            debug!("Publisher received ok");

            let mut send_buffer = BytesMut::with_capacity(4096);
            send_buffer.put_slice(b"hello\r\n");
            let ptr = send_buffer.as_ptr();
            let to_send = send_buffer.len();
            let send_e = opcode::Send::new(types::Fd(fd), ptr, to_send.try_into().unwrap());
            let ud = add_callback(move |_res| {
                info!("Publisher sent message res={res}");
                Ok(())
            });
            uring::submit(send_e.build().user_data(ud.into()))?;
            Ok(())
        });
        let e = read_e.build().user_data(ud).into();
        uring::submit(e)?;
        Ok(())
    });
    uring::submit(send_e.build().user_data(ud))?;
    Ok(())
}
```

Just adding a `let _s = send_buffer;` in the callback keeps the BytesMut alive and allocated so the pointer stays valid for the syscall.

#### Load testing

I think I'm going to try to set up the scaffolding for load testing N publishers -> M subscribers next.

You always find way more interesting issues when you start introducing load tests. Like what if a publisher sends more than 1 message? Maybe that will be a problem when your buffer pool is an association between channels and buffers and everything is async. Maybe you could have more than one buffer per channel in flight? Crazy talk.

Coming back to the state machine code for the pubsub server is extremely hard to read through. It would be so much better in the callback style, even if it was just doing:

```
let accept_e = opcode::MultishotAccept(...);
let ud = cb.add_callback(move || {
    ...
});
uring::submit(accept_e.user_data(ud));
```

Would make it sooo much easier to understand. Instead I'm like wtf does ["then this is a subscribe send"](https://github.com/benhirsch24/iouring_bench/blob/main/src/connection.rs#L411) mean? There's all this internal buffer state management, it gets quite confusing. There's also probably no buffer management so things will overflow. It might be easier if I work on re-writing this in callback style.

Oh, yeah this is confusing. Because the server is managing connections for both subscribers and publishers the publisher path of the code writes to the subscriber fd and then this is a completion event for that fd. But then I have no way of identifying which buffer this is in response to because the user data only contains the file descriptor and the operation, but not which operation. Oof, ok yeah I just have to bite the bullet and rewrite it.

It would be cool to try multishot receive for subscribers as we know they will be getting lots of receives, but I can imagine it could be complicated. I could imagine there's a situation where there's a receive but it doesn't cleanly get the whole buffer (ie instead of "hello\r\n" you get "hel") so the callback you pass in needs to be able to understand that. Also I think you're registering a group of buffers with the kernel so you'll need to be able to read data across (potentially) multiple buffers. Could get tricky. At some point.

Anyways, next I'm going to write a buffer pool because I think it shouldn't be too hard now. Planning to use a `Slab<Buffer>` where `Buffer` is refcounted and uses Bytes under the hood.

**[Deep thoughts on writing new code](#deep-thoughts)** There's a couple of different ways to write a new library API. One way is to write out all of the library code. If you're defining a BufferPool, you could start by writing src/buffers.rs

```
pub struct BufferPool {
...
}

impl BufferPool {
  pub fn get_buffer(&mut self) {}

  ...etc
}
```

I find that to be difficult because then I have to upfront figure out all of the APIs when writing the library.

Instead what I like to do is write the code that uses the library (which doesn't exist yet). I find that's the best way for me to define the ergonomics of the library. And Rust's `unimplemented!()` macro makes that super simple as you can stub out library APIs while still doing your core compile loop.

I'll sketch out my application code:

```
let publisher = Publisher(...);
let subscribers = vec![...bunch of subscribers...];
let pool = BufferPool::new();
// ...assume connection setup...
// Get message from publisher
let buffer = pool.get_buffer();
let read_entry = opcode::Recv(publisher.fd, buffer);
submit_entry(read_entry, move || {
    for s in subscribers {
      let buf = buffer.clone(); // refcounts the buffer
      let send = opcode::Send(subscriber.fd, buf);
      submit_entry(send, move || {
        // Make sure buf stays alive at least this long.
        // Ok buf was sent
        println!("Done!");
        // after last reference pool frees buf
        pool.done(buf);
      })?;
    }
})?;
```

This feels pretty good. Now ideally I wouldn't call `pool.done(buf)`. Ideally buf would go out of scope and then on the last reference it would get freed in the pool. But that would imply that Buffer has a reference to the pool so the buffer drop implementation can decrement the buffer refcount in the pool. But that feels complicated, so let's start here.

Started out with an implementation like this:

```
use bytes::{BufMut, Bytes, BytesMut};
use slab::Slab;
use io_uring::{opcode, types};
use crate::uring;
use crate::callbacks::*;

use log::info;

use std::cell::RefCell;
use std::os::fd::RawFd;
use std::rc::Rc;

#[derive(Clone)]
pub struct Buffer {
    bytes: BytesMut,
    key: usize,
}

impl Buffer {
    pub fn new(key: usize) -> Self {
        Buffer {
            bytes: BytesMut::with_capacity(4096),
            key,
        }
    }

    pub fn starts_with(&self, s: &str) -> bool {
        self.bytes.starts_with(s.as_bytes())
    }

    pub fn set_len(&self, l: usize) {
        unsafe { self.bytes.set_len(l) };
    }

    pub fn put_slice(&mut self, s: &[u8]) {
        self.bytes.put_slice(s)
    }

    pub fn send<F>(&self, fd: RawFd, f: F) -> anyhow::Result<()>
        where F: FnOnce(Self, i32) -> anyhow::Result<()> + 'static
    {
        let ptr = self.bytes.as_ptr();
        let op = opcode::Send::new(types::Fd(fd), ptr, self.bytes.len() as u32);
        log::info!("send bytes: {}", self.bytes.len());
        let ud = add_callback(move |res| {
            f(self, res)
        });
        Ok(uring::submit(op.build().user_data(ud))?)
    }

    pub fn recv<F>(&mut self, fd: RawFd, f: F) -> anyhow::Result<()>
        where F: FnOnce(Self, i32) -> anyhow::Result<()> + 'static
    {
        let ptr = self.bytes.as_mut_ptr();
        let op = opcode::Recv::new(types::Fd(fd), ptr, self.bytes.capacity() as u32);
        let ud = add_callback(move |res| {
            f(self, res)
        });
        Ok(uring::submit(op.build().user_data(ud))?)
    }
}

#[derive(Clone)]
pub struct BufferPool {
    inner: Rc<RefCell<BufferPoolInner>>,
}

impl BufferPool {
    pub fn new() -> BufferPool {
        BufferPool {
            inner: Rc::new(RefCell::new(BufferPoolInner::new())),
        }
    }

    pub fn len(&self) -> usize {
        self.inner.borrow().slab.len()
    }

    pub fn get_default_buffer(&mut self) -> Buffer {
        self.inner.borrow_mut().get_default_buffer()
    }
}

pub struct BufferPoolInner {
    slab: Slab<Buffer>,
}

impl BufferPoolInner {
    pub fn new() -> BufferPoolInner {
        BufferPoolInner {
            slab: Slab::with_capacity(4096),
        }
    }

    pub fn get_default_buffer(&mut self) -> Buffer {
        let entry = self.slab.vacant_entry();
        let key = entry.key();
        let buffer = Buffer::new(key);
        entry.insert(buffer.clone());
        buffer
    }
}
```

But it looks like BytesMut::clone is a deep copy, not a refcount. Which makes sense, can't have two refcounts to a mutable buffer.

While I was doing a Peloton ride I was thinking that putting recv/send on the Buffer doesn't make much sense, so I'll try to wrap the TCP Conn in it instead. That way I just pass the BytesMut itself into the callback which makes more sense.

Is highlighting my failures useful in a worklog like this? Idk. Hopefully maybe to someone (or myself).

### Good pattern - net contaienr

As I described in the previous section it turns out that putting the responsibility for the reads/sends on the TcpStream itself is much better (obviously). It starts to feel a little more like the actual TcpStream. Turns out the people who are working on Rust are pretty smart.

Also major props to anyone who writes a full server from scratch. There is so much to think about when you start getting into it.

One thing I'm running into is code like this:

```
let listener = unet::TcpListener::bind("0.0.0.0:8080").expect("tcp listener");
listener.accept(move |fd| {
    let stream = unet::TcpStream::new(fd);
    println!("Accepted");
    let mut protocol = vec![0u8; 1024];
    stream.recv(protocol.as_mut_ptr(), protocol.capacity(), move |res| {
        info!("Protocol res={res}");
        let msg = std::str::from_utf8(protocol.as_ref()).unwrap();
        info!("Got msg {msg}");
        if protocol.starts_with(b"PUBLISH") {
            info!("Publisher");
            // TODO: What if there's more data in the buffer than just "PUBLISH ch\r\n" ?
            send_ok_and_then(stream, |res| {
                let channel = parse_channel(protocol)?;
                info!("Publisher sent ok channel={channel}");
                relay_messages(stream);
                Ok(())
            })?;
        } else if protocol.starts_with(b"SUBSCRIBE") {
            send_ok_and_then(stream, |res| {
                info!("Subscriber sent ok");
                Ok(())
            })?;
        } else {
            send_error_and_then(stream, "not a valid protocol".into(), |res| {
                info!("And then done");
                Ok(())
            })?;
        }
        Ok(())
    })?;
    Ok(())
})?;
```

Specifically I'm highling the TODO after parsing the protocol. Right now I'm writing callbacks for every logical message that might be traded which could be true when playing around with `nc`. But realistically with a load test the sender may write multiple messages to the socket and the server may get multiple messages all at once. I'll need to instead fill up a buffer (which could continue past another read call if there's still data) and then issue reads with callbacks to get more data... ok, one thing at a time. Get it working dumbly first, then add smarts. I've never implemented a server this deeply from scratch before so this is pretty interesting, even fun.  Props to my wife for letting me explain this to her for 5 minutes today.

Hahaha and I literally start encountering this immediately. My "PUBLISH ch\r\n" receive on the server is sometimes returning just 10 characters instead of the full 12. So I'll need to deal with this now. Sigh.

So I want to do something like

```
let recv_buffer = BytesMut::with_capacity(1024);
conn.recv(recv_buffer, move |res| {
  recv_buffer.set_len(recv_buffer.len() + res);
  if let Some(line) = read_line(recv_buffer) {
    // do stuff with message
  } else {
    conn.recv(recv_buffer + res, move |res| {
      // Call exactly this function again
    });
  }
});
```

Let's go one more level

```
let recv_buffer = BytesMut::with_capacity(1024);
conn.recv(recv_buffer, move |res| {
  do_message(buffer, res);
});

fn do_message(buffer: BytesMut, res: i32) {
  // optionally grow the buffer
  buffer.set_len(buffer.len() + res);
  if let Some(line) = read_line(recv_buffer) {
    // do stuff with message
  } else {
    // No line available
    conn.recv(recv_buffer, move |res| {
        do_message(buffer, res)
    });
  }
}
```

I guess that's not too bad? Probably since everything I have is based on lines delimited by carriage returns I just want some sort of `read_line` function that then finally calls a callback once it has a line.

That implies I probably want something that has an internal buffer to buffer many bytes... sigh. I'm definitely re-inventing so many things right now but I don't really want to read other people's code just yet (or ask ChatGPT/Claude to actually code for me yet).

### 10/17 Comparable frameworks {#research}

Busy week at work + busy personal week meant no personal work time.

I did spend a couple of nights reading through some code for [Seastar](https://docs.seastar.io/master/tutorial.html#introducing-seastars-network-stack) which is a C++ framework for thread per core high performance applications, and glommio which is a Rust framework built around `io_uring` and has its own async framework.

Seastar has a very similar style to what I've been writing. For example this server:

```
seastar::future<> service_loop() {
    seastar::listen_options lo;
    lo.reuse_address = true;
    return seastar::do_with(seastar::listen(seastar::make_ipv4_address({1234}), lo),
            [] (auto& listener) {
        return seastar::keep_doing([&listener] () {
            return listener.accept().then(
                    [] (seastar::accept_result res) {
                auto s = std::move(res.connection);
                auto out = s.output();
                return seastar::do_with(std::move(s), std::move(out),
                        [] (auto& s, auto& out) {
                    return out.write(canned_response).then([&out] {
                        return out.close();
                    });
                });
            });
        });
    });
}
```

Each async call returns a future which is chained with `.then(...)` which takes a callback. The future, when ready, passes the results to the callback.

Started implementing a buffered reader. Now that I look at the Seastar example I think that could be an easier intermediate step between what I've got now and the full async runtime. Plumbing the callback to each function is very annoying when I could instead return a Future (but I wouldn't call it a future to not conflict when I bring futures in, I'll call it an `Urture`) and then chain results with `.then(...)`. Will think about doing that maybe Sunday if I have time.

### Going async {#async}

I went down the path of writing my own futures to clean things up like I said... but then I went fuck it, I'm going to write an async runtime. I think I've been dancing around it long enough and I think it shouldn't be that bad to do the bare minimum to get things working.

It's 10:45pm and I'm a little bleary eyed, I have to be on a war room tomorrow as the on-call for a customer's large event (yes, even as a Principal Engineer I stay on the rotation), but 15 minutes of coding can't hurt.

### 10/19

I spent some of yesterday and a bit of today working on my async runtime. I used Claude as a rubberduck / teacher which I thought really helped. [Here's my conversation](https://claude.ai/share/ad2d1f01-d8d3-43c6-a0a6-26f9b91912e2).

Now I don't think that Claude could have written my runtime for me. I had to do a lot of back and forth, both for my own understanding (it was actually pretty good at telling me where and why I was wrong with some assumptions) but it also made a lot of simple mistakes that I could catch and clarify with it.

Without Claude I probably would have had to read more code from tokio or smol-rs or glommio to understand how to set up an async runtime. I could learn like that (and I still did quite a bit of that), but in a certain sense Claude, having been trained on the internet/GitHub, is essentially remixing the patterns that it understands from these established async runtimes so I'm learning by proxy. Or something like that.

Implemented AsyncRead and AsyncWrite for TcpStream. Once I did that, I was able to just `use futures::{AsyncReadExt, AsyncWriteExt}` and it all just worked. I have an example program working like this:

```
    executor::spawn(Box::pin(async {
        let mut listener = unet::TcpListener::bind("0.0.0.0:8080").unwrap();
        loop {
            let task_id = executor::get_task_id();
            info!("Accepting");
            let mut stream = listener.accept_multi_fut().await.unwrap();
            executor::spawn(Box::pin(async move {
                let task_id = executor::get_task_id();
                info!("Got stream {} for {task_id}", stream.as_raw_fd());
                loop {
                    let mut buf = [0u8; 1024];
                    let n = stream.read(&mut buf).await.expect("Read buf");
                    info!("Read {n} from stream");
                    if n == 0 {
                        info!("Stream done");
                        return;
                    }

                    let response = "hello to you too\r\n";
                    let mut written = 0;
                    while written < response.len() {
                        let n = stream.write(response.as_bytes()).await.expect("Write");
                        written += n;
                        info!("Wrote {n}");
                    }
                }
            }));
        }
        ()
    }));
```

This is actually so cool and so much easier than what I was doing before. I'm a fool for not just doing this immediately lol. But I think going through the process of state machine -> callback hell -> async runtime was actually a very worthy one. Now I feel like I have a really good intuition for how it all fits together. Plus I think it's cool that the repo has all of the pieces to write either state machine or callback or async code. I should write an article that shows each style and how to build up the async runtime, I think that would be a super useful article.

The whole async runtime only took me ~5 hours max if I were to guess. 2 hours last night while on an event call for work (on-call), 1 hour this morning while my wife got herself and Henry ready for a park day, and 2 hours just now after I got back from said park day. This session was mostly about getting all the pieces I had in place actually working, namely the uring opcode completion to task id association machinery working. Once that was done I could implement AsyncRead/AsyncWrite and things Just Worked.

Cool, now I should actually do the pubsub server and finish what it was I was actually trying to do lol.

Commits:

[Super basic executor sketch](https://github.com/benhirsch24/iouring_bench/commit/e8b99498f06b05025f5179f14ce6f905df930be5)

The key insight, as from my conversation with Claude above, is that each completion event will lead to polling a Future. Rather than trying to force the result into the future somehow, you can just store the result (since it's an i32 for syscalls) and then the Future will retrieve it. IIRC the AcceptFuture wasn't working yet in this commit, but I had a basic unit test that was.

[First successful accept](https://github.com/benhirsch24/iouring_bench/commit/a65103478a6cf81c9474fe96163e7680c3b6ea6a)

Here I had to manually plumb task IDs around and associate them with operation ids (which are passed in the user data). This was clunky, but would work once. Because I was creating a new AcceptFuture every time `accept_multi` was called the future would only ever be ready once. But still, this shows how a future could get the result from the executor which stored the i32 result. This is fine since our runtime explicitly is planned to be thread-per-core (hence the liberal use of UnsafeCell).

[Multi accepts working](https://github.com/benhirsch24/iouring_bench/commit/39dcc2e1533ac1e3362cc6b7162eb7b588f8371a)

Here I added a thread local global variable for the current task id. Now the executor calling code doesn't have to come up with a task ID when scheduling operations or tasks, it can just "do it" and the executor will handle the details. Also the multi accept works because I added a bool into the hash map to keep that state.

[AsyncRead and AsyncWrite](https://github.com/benhirsch24/iouring_bench/commit/96fb1eef5e86b1495e36bb166f5552346602f659)

Pretty easy once you get the hang of it. I don't think I need a flush implementation (can always revisit). Also translating from the raw os error to a Result is nice. But this just made me realize that res is negative so I need to fix that real quick.

### Implementation Done?

Wow is my pubsub implementation done? It took me a couple more hours to do today but I think so.

[Timeouts](https://github.com/benhirsch24/iouring_bench/commit/abc9e7cd54391c6102451d521af0ce27b110d42d)

The timeouts implementation was a little weird because you have to have a pointer to a Timespec living somewhere and obviously if you have a TimeoutFuture that gets consumed that's only valid once.

I looked at the glommio source code and took inspiration from it, I made a hashmap in the executor with timespecs and then get the pointer to the value. Then you can register a timer with the executor. I don't like having the io_uring crate types leak into the executor, but I think this should be fine. Having the 5 second heartbeats with uring metrics is really familiar and nice to me.

[Full pubsub implementation](https://github.com/benhirsch24/iouring_bench/commit/17441ac4c35b6b65502581267f43e87082e71372)

This was actually way easier than I thought. It's 11:30PM on a work night (Sunday) but I had to get it done once I got in a rhythm. I fed Henry his bottle, he's doing pretty well at it now. Then I finished it all up in an hour and wanted to jot down some thoughts here before going to bed.

In the end I was right: writing the full async runtime was the right move. There's obviously a reason why Rust has gone this direction, it makes expressing programs SO much clearer when you can write synchronous-looking code even if behind the scenes it's not.
