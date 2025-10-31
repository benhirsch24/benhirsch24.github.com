---
Date: 2025-10-30
---

# IO Uring worklog part 4

In this edition I fixed the last issues in my async executor that I could discover from load testing my client and server locally.

Reading through what I've written it's actually much harder to understand this worklog than previous ones that I've written. This one was basically all about getting bugs shaken out from my async runtime which I did by writing a pubsub client for my pubsub server from last time. From there I started doing load tests which exposed a lot of bugs. There's some bugs you can find by doing one publisher to one subscriber, but there's some bugs you only find by trying 5 publishers to 25 subscribers at 50 messages per second.

Some of my favorite bugs that I fixed:

* Nested `Pin<Pin<Pin<...Pin<impl Future>>>>` from re-`Box::pin()`-ning my tasks every time I put them back into the task map
* Really not thinking through the lifetime of memory of timespecs and sockaddrs when I pass pointers to io_uring
* `socket2::Socket` gets closed when dropped, so keep that around
* Calculating intervals for a constant transactions per second takes more than a second of thought
* Turns out that iterating over a Drain iterator but also pushing things into the underlying Vec leads to unexpected results

Anyways, now that I feel pretty good about both my async implementation and the client and server I can get back to load testing. I'll have to implement the same thing in Tokio of course to get an apples to apples comparison.

By the by, I found this old boats post about [Why async Rust?](https://without.boats/blog/why-async-rust/) which literally covers the journey I went on through writing this async runtime, which I find very funny.

## Work log

### Load Testing client

Writing the `connect` future legitimately took longer than writing the client itself.

```
    pub fn connect<A: ToSocketAddrs>(addr: A) -> ConnectFuture {
        let op_id = executor::get_next_op_id();

        let socket_addr = addr.to_socket_addrs().ok().unwrap().next().unwrap();
        let os_socket_addr = Box::new(os_socketaddr::OsSocketAddr::from(socket_addr));
        let socket = Socket::new(Domain::IPV4, Type::STREAM, Some(socket2::Protocol::TCP)).unwrap();

        let fd = socket.into_raw_fd();
        let ptr = os_socket_addr.as_ptr();
        let len = os_socket_addr.len();
        let opcode = opcode::Connect::new(types::Fd(fd), ptr, len);
        executor::schedule_completion(op_id, false);
        trace!("Scheduling connect completion for op={op_id} fd={} task_id={}", fd, executor::get_task_id());
        if let Err(e) = uring::submit(opcode.build().user_data(op_id)) {
            log::error!("Error submitting connect: {e}");
        }
        ConnectFuture { op_id, addr: os_socket_addr, fd }
    }
```

Some of the issues I had (laugh at me, it's ok):

Because we're doing io_uring the `*const struct sockaddr` needs to be valid until the completion event comes back. You can't allocate it on the stack in the connect function as that memory is gone when you return. You can't put it in ConnectFuture on the stack and point it to that address for the same reason. So you have to Box it up into the ConnectFuture.

Took me a while to figure out that Socket was closing the socket on drop. It would return successfully and my runtime machinery looked right, but writing to it would say it was a bad file descriptor. I ran `strace` and saw that it was clearly being closed before the uring was entered which is what I suspected, that `socket2::Socket` was closing it on drop. I kept looking at [the source](https://docs.rs/socket2/latest/src/socket2/socket.rs.html) and couldn't find any `impl Drop` until I found that `sys::Socket` is a [type alias to an OwnedFd](https://docs.rs/socket2/latest/src/socket2/sys/unix.rs.html#862) which TIL what an OwnedFd is. Makes sense, easy fix with `IntoRawFd` and hopefully I'll remember that for the future.

I saw [post on Hacker News about a fast web server](https://news.ycombinator.com/item?id=45653350) which pointed me to [monoio](https://docs.rs/monoio/latest/monoio/), a Rust crate which uses io_uring (or epoll). Looks like it's built by ByteDance, I'll have to take a look at the code later to see how they do things.

#### Got it working 10/21

There was a particularly hairy bug with how I was handling the futures returned by `poll_read` or `poll_write`. I was having the future immediately take the op id out of the Option<> in the TcpStream, but that meant that the first time it was polled before the completion event fired it would return pending BUT there was no result yet. [I had a long chat with Claude](https://claude.ai/share/b83401a9-871e-417e-9ce5-8231d0ed76fe) which was semi-helpful. It got a lot of things wrong, but I was able to rubber duck with it enough to get to the right answer myself. It's good at knowing a lot of things, but not knowing my program. Part of me wonders if I'm giving up brain power to it for things like this, but I actually think chatting with it like this helped me debug by explaining myself to it. It definitely **did not** get it correct just by me copy pasting code in.

Also first game of the NBA season, Lakers baby!

Next up I have to deal with borrowing an `Rc<RefCell<HashMap<...>>>` across a Future `.await` point. I'm re-discovering all the classic problems that you run into when writing an async runtime, I've totally read other blog posts and comments of people talking about these exact problems. Maybe I'll end up writing channels to deal with this. I could probably pretty easily write an spmc future that checks whether the message was polled by all the consumers. Or something like that, idk. Will figure it out.

I just got around it by a shitty, dumb method: because I create TcpStreams by fd all over the place, I just copied all the Fds and remade the TcpStreams lol. This is not a production framework at all, this is for my own enjoyment (weird enjoyment tbh).

Once I started benchmarking it on my Mac on a VM I noticed that at 5 publishers with 50 subscribers each and 50TPS for publishers my program starts to slow down and the TPS creeps down to ~10. When I ran pidstat I found that it's spending almost 100% of time in userspace. Doing a perf record and flamegraph gave me the following SVG (does this embed?):

![Flamegraph of lots of polls](/images/flamegraph-polls.svg)

That's a lot of polling. Also my server eventually does stack overflow, so there must be something I'm doing weird with it...

Oh! I think because I'm doing

```
for s in streams {
  s.write_all(buffer).await
}
```

I'm waiting for each write to complete before doing the next one. I conceptually was thinking of this as "queue up a write to every stream, then await them all". So I probably now need to start returning join handles from executor::spawn to do each write concurrently :)

Or maybe I don't even need to do this join, I can just spawn and await in the spawned task. Nice that worked for the most part!

This is what Claude says about it:

```
What Was Happening Before

When you had:

rustfor mut s in streams {
    s.write_all(line.as_bytes()).await;
}
The compiler desugars this into something conceptually like:

rust// Pseudocode of what the compiler generates
match streams[0].write_all().poll() {
    Ready => match streams[1].write_all().poll() {
        Ready => match streams[2].write_all().poll() {
            Ready => match streams[3].write_all().poll() {
                // ... 50 subscribers deep!
            }
        }
    }
}

Each .await in a loop creates a new state in the generated state machine, and with 50 subscribers, you had 50 nested future states all composed together. The future for the entire loop body becomes a massive nested structure.
```

Which makes sense. I'll have to spend some more time understanding how async sugaring works later.

This potentially loses the ordering of writes, maybe. Everything is pretty orderly in that tasks get woken up, placed on the back of the queue, then we work our way front to back. It would still potentially make sense to have a task with a channel per subscriber that receives messages in order and writes to the underlying socket, but again I don't want to await each message to fully write, I just want to queue it up. I think.

### TPS problems

Now I'm seeing that we're not always getting 50 TPS per subscriber. It's usually around ~40-45. Also there's always a few errors at the start.

Once I started adding debugging I noticed that the server is only ever receiving messages for one publisher.

Adding logging mostly took place on 10/21, then I went to NYC. My 3 month old son did great on the plane. Then I came back on 10/27 and basically figured out the issue.

I stepped back and worked through the problem by adding a ton of tracing and went to 2 publishers each with 2 subscribers at 1TPS per publisher.

The logs looked like this:

```
[2025-10-28T03:45:10Z TRACE iouring_bench::uring] completion result=30 op=15
[2025-10-28T03:45:10Z TRACE iouring_bench::executor] handle_completion op=15 res=30 task_id=4 is_multi=false
[2025-10-28T03:45:10Z TRACE iouring_bench::uring] completion result=30 op=18
[2025-10-28T03:45:10Z TRACE iouring_bench::executor] handle_completion op=18 res=30 task_id=7 is_multi=false
[2025-10-28T03:45:10Z TRACE iouring_bench::executor] Ready queue len 2: [4, 7]
[2025-10-28T03:45:10Z TRACE iouring_bench::executor] Set task_id=4
[2025-10-28T03:45:10Z TRACE iouring_bench::net] Polling 15
[2025-10-28T03:45:10Z TRACE iouring_bench::executor] Removed op=15
[2025-10-28T03:45:10Z TRACE iouring_bench::net] Got recv result 30 op_id=15
[2025-10-28T03:45:10Z TRACE iouring_bench::net] Ready 15 30
[2025-10-28T03:45:10Z DEBUG pubsub_server_async] Got message here is my message Channel_1
     channel=Channel_1 fd=8 task_id=4
[2025-10-28T03:45:10Z TRACE iouring_bench::net] Scheduling recv completion for op=20 fd=8 task_id=4 len=8192
[2025-10-28T03:45:10Z TRACE iouring_bench::executor] Task still pending 4
[2025-10-28T03:45:10Z TRACE iouring_bench::executor] Set task_id=9
[2025-10-28T03:45:10Z TRACE iouring_bench::net] Scheduling send completion for op=21 fd=9 task_id=9 len=30
[2025-10-28T03:45:10Z TRACE iouring_bench::executor] Task still pending 9
[2025-10-28T03:45:10Z TRACE iouring_bench::executor] Ready queue len 2: [8, 9]
```

Where tasks 4 and 7 were both my publisher tasks and a result of 30 meant it received my message `here is my message Channel_N\r\n`. Task 4 got processed for `Channel_1`, but then the next time there was a task switch it went to 9 instead.

This was due to a bug in my ready_queue handling. This seems to be because I was draining the vector and using it in the iterator like this:

```
    fn handle_ready_queue(&mut self) {
        trace!("Ready queue len {}: {:?}", self.ready_queue.len(), self.ready_queue);
        for task_id in self.ready_queue.drain(..) {
            set_task_id(task_id);
            trace!("Set task_id={task_id}");
            if let Some(mut task) = self.tasks.remove(&task_id) {
                let mut ctx = Context::from_waker(Waker::noop());
                match task.as_mut().poll(&mut ctx) {
                    Poll::Ready(_) => {
                        trace!("Task {task_id} complete");
                    },
                    Poll::Pending => {
                        trace!("Task still pending {task_id}");
                        self.tasks.insert(task_id, Box::pin(task));
                    },
                }
            }
        }
    }
```

The fix was to drain the queue first, then iterate like this:

```
    fn handle_ready_queue(&mut self) {
        trace!("Ready queue len {}: {:?}", self.ready_queue.len(), self.ready_queue);
        let tasks: Vec<u64> = self.ready_queue.drain(..).collect();
        for task_id in tasks {
            set_task_id(task_id);
            trace!("Set task_id={task_id}");
            if let Some(mut task) = self.tasks.remove(&task_id) {
                let mut ctx = Context::from_waker(Waker::noop());
                match task.as_mut().poll(&mut ctx) {
                    Poll::Ready(_) => {
                        trace!("Task {task_id} complete");
                    },
                    Poll::Pending => {
                        trace!("Task still pending {task_id}");
                        self.tasks.insert(task_id, Box::pin(task));
                    },
                }
            }
        }
    }
```

It seems like there is [lots of unsafe code in the drain iterator implementation](https://doc.rust-lang.org/src/alloc/vec/mod.rs.html#2771-2773) where it's basically creating pointers into the vec data. But when I poll tasks I'm also adding data into the ready_queue vector; that combined with my usage of UnsafeCell means the drain iterator ends up seeing a new task that's ready and some of my tasks get missed.

Simple fix in the end that was easy to debug, it just took getting ~1 hour of free time on the couch (kinda impossible on a plane with an infant unfortunately).

#### What about now

There's still some hairy issue deep in there (ew). One: I'm not getting 25TPS with 2 publishers and 2 subscribers and that surprises me. And it's always off by like 1 message per second. I would not expect that 25TPS is a limit even on a VM. Two: When I went up to 5 publishers, 25 TPS, 25 subscribers per publisher I ran into Channel 3 and Channel 4 getting starved again. So there's still some sort of concurrency bug that manifests at scale.

Ugh but debugging this is a pain because there'll be so many logs... I need to develop something to parse them. Probably something could be vibe coded, idk. I haven't vibe coded almost anything for this project because it feels like I need to actually understand what I'm building end-to-end, and if I let AI do anything then I'll end up unloading context. I probably should make a program that helps debug this, but again this isn't a production library it's fun. Or something. Idk why I'm still working on this but it's enjoyable.

I started debugging anyways even if it's annoying and it's 10pm and I should go to sleep soon. I think this is timeout implementation related on the client side, the issue is no longer in the server side. Once I went through trace logs I noticed that timeouts stopped firing for the tasks that stopped writing.

Ok yeah, my timespec implementation was messed up, I was returning a pointer to a timespec in a HashMap but the memory location of those timespecs could move. Simple solution is to Box them. After that, my client seems to be happy. My server side still looks like it's undercounting messages by just a bit.

```
[2025-10-28T05:26:07Z INFO  pubsub_server_async] Uring metrics: submitted_last_period=0 completions_last_period=1441 submit_and_wait=97 last_submit=53263us
[2025-10-28T05:26:07Z INFO  pubsub_server_async] Channel_0 sent 240
[2025-10-28T05:26:07Z INFO  pubsub_server_async] Channel_1 sent 240
[2025-10-28T05:26:07Z INFO  pubsub_server_async] Channel_3 sent 240
[2025-10-28T05:26:07Z INFO  pubsub_server_async] Channel_4 sent 240
[2025-10-28T05:26:07Z INFO  pubsub_server_async] Channel_2 sent 240
```

With 5 publishers at 10TPS and 5 subscribers per publisher, with the server emitting metrics every 10 seconds, I should be seeing 5 * 5 * 10 = 250 messages per channel per 10 seconds. The messages aren't falling into a separate 10 second bucket, the whole 30 second test doesn't add up. There's probably one more thing in the server that needs to be figured out.

10/28: This is definitely on the client side, not the server side. When I instrument my client application with metrics I see that at 1TPS I see the expected number of writes, but at just 5TPS I'm missing some writes. I'm not sure what's up with that, I wouldn't expect to see that fall off like that.

Ok it looks like it takes about 1ms per task. My ready queue says it gets handled in ~10ms for 10 tasks. That feels long...

Doing some profiling, it seems like I have a deep poll stack depth again. And a lot of them are in the read line reader stuff. I may have to look into optimizing that tomorrow.

**Tomorrow**:

I added some timing around how long tasks take and they can take up to 900us! That seems very long for what should just be creating an opcode around a buffer. For writing there is string creation/allocation since I'm writing different messages each time.

Oh wow, in debugging with Claude I (it) realized that by re-`Box::pin`-ning the task every time I put it back in my map I was building hugely nested layers of Futures. Just sticking the task back reduced the time taken per task by over half! [Here is the conversation for transparency](https://claude.ai/share/58dce16f-dd48-4489-9c07-e63389fb054d). This one I truly did end up pasting code and having it find the problem after I guided it by giving it context. I gave it profiles and some code.

Seems like this was valid because [Pin implements Future](https://doc.rust-lang.org/stable/std/pin/struct.Pin.html#impl-Future-for-Pin%3CP%3E) so technically `Pin<Pin<impl Future>>` is a `Pin<Future>`.

Between that and fixing my TPS calculation we're now achieving the TPS I'm expecting.

[Here's the commit that fixed the things](https://github.com/benhirsch24/iouring_bench/commit/95959c0d0017ee613e26860d731f4a211a8e9ae5)

Here's before:

![Before removing extra pin](/images/iouring-2025-10-29-tps-debugging.svg)

And here's after:

![After removing extra pin](/images/iouring-2025-10-30-no-more-pins.svg)

Now, if necessary, I can start to pick off more higher hanging fruit like the HashMaps I'm using to store timespecs, or how I'm allocating Tasks. But it is cool to remove such big bottlenecks!
