---
Date: 2025-10-12
---

# IO Uring Worklog Part 2

I'm actually really enjoying writing these worklogs. It feels much lower pressure than a full blog post where I'd have to like... edit. But this also allows me to write my thoughts down and refer back to them. And it's a cool way to see the progress I've made. These are all very raw.

I'll try to keep summarizing in the first section. Here's a ChatGPT summary of this worklog which is unfortunately very accurate:

I spent a week turning an io_uring toy server into a batching lab: I implemented an adaptive strategy that busy-polls with small batches under load and parks with submit_and_wait when idle, added histograms to see what it’s doing, and built a fan-out PUB/SUB prototype to create real opportunities to coalesce syscalls. Along the way I dove through kernel paths for socket reads, wired up tracepoints/kprobes, refactored the event loop with a thread-local ring and a callback registry (escaping borrow checker fights with UnsafeCell in this single-threaded design), standardized on Bytes/BytesMut, and sketched a buffer pool and a cleaner Server API. Next up: parameterized load tests, buffer registration experiments, and head-to-head benchmarks against Tokio/Glommio to see how much syscall coalescing really buys us.

I'll add: I think the most interesting bits are the Abstraction and Pubsub clients with state machine and callbacks. If you've ever wondered about writing a runtime around an async system you can see me start to evolve one by the end. I may spin this out into a separate post at some point (more dedicated than a worklog).

## Table of Contents

[Understanding IO Uring more](#understanding) - Used Codex to help me read through Linux source code and understand how IO Uring works under the hood. Also thought through how to optimally batch entries to the submission queue to balance latency versus excessive syscalls. Started thinking about a use-case where I could see if IO Uring could really shine - pubsub server with subscribers >>> publishers (high fan-out).

My basic understanding of how IO uring is something like: In non-SQPOLL mode when I submit syscalls in a batch with `io_uring_enter/submit` the kernel will try to do each of these syscalls and create a task for them (io uring task). It will try to execute these syscalls against the fd in async / non-blocking mode. When network packets arrive on the NIC it interrupts the CPU which tells the kernel to do something with the packets. The kernel does something with them and then does a "wake" of the io task. Next time io uring enters (or next time the kernel tasks run) it can put an event on the completion queue that there is data ready. In SQPOLL mode you have a dedicated kernel thread polling. This can reduce latency for syscalls (no waiting for kernel task scheduling) but at the cost of a CPU busy-polling the whole time. Still, in latency sensitive applications this can be useful because your application doesn't need to spend time switching between kernel and userspace. I'll have to explore more of the other modes.

[Beginning Abstraction](#abstraction) - Here I started to abstract things out more. I pulled IoUring logic out into its own module. I also highlighted some interesting presentations from NetDevConf that I found while googling information.

[Completed abstraction](#more-abstraction) - Better finishing place of abstraction for uring core. Now code can call `uring::submit(opcode)` with an opcode to submit from anywhere instead of passing a handle to the uring around everywhere.

[Pubsub Server](#pubsub-server) - I wanted to test out the batching idea from before by writing a pubsub server in main.rs. Defined a very simple protocol, thought through sending a single buffer to each subscriber, a little abstraction.

[Pubsub Client with State Machine](#dogfooding) - To test my Pubsub Server I decided to write a pubsub client using IO Uring instead of doing it with Tokio or in Go or something. Got something working pretty quickly in one night which was cool.

[Pubsub Client with callbacks](#callbacks) - Without an async runtime the completion model can get tricky. You have to define a state machine to transition at every completion and decide what to do next. Here I decided to re-write the pubsub client but where I register callbacks for each completion event. This ends up being a lot nicer to work with, although not quite as nice as async/await.

## Worklog

10/04 - 10/06ish aka working in Tahoe in between excursions with my wife and son because I'm now obsessed.

Trying a new way with batching requests where I `submit_and_wait` if I haven't submitted for a period of time.

I think another cool thing I could try is a dynamic batch size. If I have a bunch of requests in flight (and realistically a bunch of talking requests, not idle ones) I could make the batch size bigger. If they slow down, so does the batch size. But I still don't have the intuition for this or what I want it to do. For low latency applications with lots of constant work I think I basically want to busy-poll with submit with small/medium batches. For less latency sensitive applications I can have bigger batch sizes... but I'd still want to batch to some extent and then submit and wait when I have no work? I don't know, still thinking it through.

I think what I'd want to do is: If I have lots of work (ie lots coming off the completion queue) then I batch up a bunch as I can into few `submit` calls. However if I do not have a lot of work, then I just submit whatever I have into `submit_and_wait`. This way if we have lots of work we make few syscalls. If we have little work then we're just queueing what we have and we park the program until the work arrives. If I busy loop (ie do not call `submit_and_wait`) then I'm hoping that more work arrives on the completion queue and enough will arrive that I can submit another batch. However if that's not the case then I'm just eating CPU looping over and over for work that doesn't arrive.

Btw multishot accept was very simple and does make the program easier though. Same with multishot timeout.

I'm adding some histogram metrics to see how long between writes I'm going. I'm also trying to add some stats around submit_and_wait so I know how many IOps I'm queueing each time there instead of in batches in submit. I feel like if I wanted to try my "adaptive batch size" idea this would be the first step.

### [Understanding IO Uring more](#understanding)

To build a better intuition with io uring I downloaded the Linux source code and then went through it with the help of OpenAI's Codex. I feel like I have a better understanding of how this is actually working (and a better understanding of Linux workings in general). Here's the transcript of our conversation. I edited some of the cruft out (where it makes a plan, explores files, etc).

I found this extremely helpful, and then going from this conversation back to the [CloudFlare Missing Manuals - io_uring worker pool](https://blog.cloudflare.com/missing-manuals-io_uring-worker-pool/) article I had a way better idea of what was going on in that article.

```
▌ read through the code in io_uring to pull it into your context. I'm specifically interested in tracing the path that requests take from submission queue (enqueued by io_uring_enter) to how they're scheduled for
▌ actual io.

> Request Flow

  - io_uring_enter resolves the ring file, handles SQPOLL wakeups, then serialises submission under ctx->uring_lock by calling io_submit_sqes whenever we have SQEs to consume (io_uring/io_uring.c:3453).
  - io_submit_sqes pulls SQEs from the shared ring (via io_get_sqe), allocates an io_kiocb, and keeps batching until either the SQ ring dries up or a failure stops the loop (io_uring/io_uring.c:2418).
  - Each SQE is normalised by io_init_req and fed through io_submit_sqe, which manages link chains and decides whether the request can run inline or must fall back to the async path (io_uring/io_uring.c:2288).
  - Opcode-specific prep/issue logic lives in the io_issue_defs table, so the eventual def->issue() invoked later knows how to submit the concrete I/O (e.g., reads/writes in io_uring/opdef.c:54).
  - After the batch, io_submit_state_end flushes deferred completions and any block I/O plug before updating the user-visible SQ head (io_uring/io_uring.c:2341).

  Async Scheduling

  - Inline execution is attempted by io_queue_sqe, which calls io_issue_sqe with IO_URING_F_NONBLOCK; success yields an immediate completion, otherwise the code drops into the async helpers (io_uring/
  io_uring.c:2067, io_uring/io_uring.c:1839).
  - io_queue_async handles the common -EAGAIN case: it copies in any SQE data and arms an async poll handler so readiness wakes the request back up, or, if polling is impossible, punts to io-wq (io_uring/
  io_uring.c:2042; poll arming in io_uring/poll.c:541 and io_uring/poll.c:694).
  - Work destined for worker threads is prepared by io_queue_iowq, which tags the entire link, records flags (hashed/unbound/concurrent), and enqueues the work item onto the appropriate io-wq account (io_uring/
  io_uring.c:499).
  - Requests that should be retried from task context—because poll fired, DEFER_TASKRUN is set, or local retries are needed—are queued via io_req_task_queue, with io_req_task_submit later resubmitting or punting
  them again under the ring lock (io_uring/io_uring.c:1476, io_uring/io_uring.c:1456).
  - io_wq_enqueue places the work on the worker pool, optionally spinning up new workers, while io_wq_worker loops pulling items, preserving hashed ordering, and re-enqueuing linked jobs as needed (io_uring/io-
  wq.c:1003, io_uring/io-wq.c:600).
  - Actual blocking submission happens in the worker context through io_wq_submit_work, which re-runs io_issue_sqe without the nonblocking flag, handles multishot/poll corner cases, and falls back to task_work
  completion on errors (io_uring/io_uring.c:1900).

  If you want to follow the execution of a specific opcode (e.g., how reads end up in the block layer), the per-op handlers in io_uring/rw.c are the next place to dig.

▌ Yes, let's trace the read opcode (IORING_OP_READ) for network sockets.

> Network Read Path

  - SQE batching funnels through io_submit_sqe, which recognizes a read opcode and queues the request via io_queue_sqe for inline, non-blocking issuance (io_uring/io_uring.c:2288, io_uring/io_uring.c:2067).
  - io_queue_sqe calls io_read, which prepares iovecs/provided buffers (io_import_rw_buffer) and sets up the kiocb before attempting the read (io_uring/rw.c:110, io_uring/rw.c:907, io_uring/rw.c:1022).
  - The actual data path is io_iter_do_read, invoking file->f_op->read_iter; for sockets that is sock_read_iter, which forwards into sock_recvmsg with MSG_DONTWAIT when io_uring set IOCB_NOWAIT (io_uring/rw.c:825,
  net/socket.c:1153).
  - On immediate success, the request completes in place via kiocb_done, updating the CQE and recycling buffers synchronously (io_uring/rw.c:649).

  Async Fallback & Polling

  - If the socket returns -EAGAIN, io_queue_async handles it: it copies any SQE data and arms socket polling rather than punting to io-wq, because sockets are pollable (io_uring/io_uring.c:2042).
  - Poll arming is done through io_arm_poll_handler, which maps EPOLLIN/POLLERR and registers the request on the socket wait queue; this is the hook that re-drives the read once data arrives (io_uring/poll.c:694).
  - When the poll fires, task_work io_poll_task_func runs; for read opcodes it resubmits the request through io_req_task_submit, which re-enters io_queue_sqe under the ring lock and retries the non-blocking read
  with data now available (io_uring/poll.c:313, io_uring/io_uring.c:1456).

  Socket-Specific Notes

  - Because sockets advertise file_can_poll, they stay entirely on the submission thread (or SQPOLL thread) and avoid io-wq unless the request was force-async for other reasons (e.g., drain/link state) (io_uring/
  io_uring.h:592).
  - Multishot reads (IORING_OP_READ_MULTISHOT) require a pollable file; io_uring enforces that for sockets and keeps the request pinned to poll-driven resubmission instead of relying on explicit re-queue (io_uring/
  rw.c:1036).
  - Provided buffers (IOSQE_BUFFER_SELECT) are fetched during import, and the sel struct records which buffer was consumed so it can be recycled if the read later punts (io_uring/rw.c:132).

  Next steps, if useful:

  1. Drill into sock_recvmsg paths (TCP vs UDP) to see how the socket backlog interacts with io_uring.
  2. Inspect io_read_mshot behaviour alongside buffer recycling to optimize high-throughput socket receivers.

▌ Could you give me a way to trace my program through io_uring functions with ebpf (or something similar)?

> Tracing Options

  - Tracepoints listed in include/trace/events/io_uring.h:1 cover key lifecycle events (io_uring_create, io_uring_queue_async_work, etc.); enumerate them with sudo bpftrace -lv tracepoint:io_uring:* and attach
  scripts that filter on your PID or ring pointer.
  - For precise function coverage (e.g., io_read, io_queue_async), attach kprobes/kretprobes; bpftrace can guard by pid == TARGET to avoid kernel-wide noise.
  - Pair the io_uring tracepoints with networking ones such as tracepoint:syscalls:sys_enter_recvfrom to correlate socket context if needed.
  - Where latency matters, collect stack traces (ustack, kstack) at io_uring:io_uring_submit_sqe and io_uring:io_uring_complete to see who submits/completes each request.

  Example Scripts

  - Quick submit/complete timeline (replace <PID>):

  sudo bpftrace -e '
  tracepoint:io_uring:io_uring_submit_sqe /pid == PID/ {
      printf("%llu submit %p op %d user_data=%lx\n", nsecs, args->ctx, args->opcode, args->user_data);
  }
  tracepoint:io_uring:io_uring_complete /pid == PID/ {
      printf("%llu complete %p res=%d flags=0x%x user_data=%lx\n", nsecs, args->ctx, args->res, args->cflags, args->userdata);
  }'

  - Follow the read-fast-path with kprobes:

  sudo bpftrace -e '
  kprobe:io_submit_sqe, kprobe:io_read, kretprobe:io_read /pid == PID/ {
      @depth[probe] = count();
      printf("%llu %s ret=%d\n", nsecs, probe, retval);
  }'

  Add kretprobe:__io_read to see retry decisions.

  - Watch async punts for sockets:

  sudo bpftrace -e '
  kprobe:io_queue_async /pid == PID/ { printf("%llu punt %p flags=0x%llx\n", nsecs, arg0, ((struct io_kiocb*)arg0)->flags); }
  tracepoint:io_uring:io_uring_queue_async_work /pid == PID/ {
      printf("%llu async %p opcode=%s\n", nsecs, args->req, str(args->op_str));
  }'

  Workflow Tips

  - Start with tracepoints for broad context, then add kprobes on bottleneck functions (io_iter_do_read, sock_read_iter) once you know where time is spent.
  - Use /sys/kernel/debug/tracing/ and ftrace (echo function > current_tracer) if you need lightweight tracing before switching to eBPF.
  - On production kernels, prefer CO-RE (bcc, libbpf skeletons) to avoid recompiling scripts across versions; the tracepoint structs in include/trace/events/io_uring.h give the field layouts you need.
  - Combine the above with tooling like perf c2c or ktap if you want CPU-side profiling while the eBPF scripts show logical flow.

  Next Steps

  1. Drop your actual ring file descriptor/PID into the scripts and confirm you see submit/complete pairs under load.
  2. Extend the kprobe script to emit ustack()/kstack() for slow completions (e.g., if (retval == -EAGAIN) { ustack(); }).
  3. If you need long-term metrics, reimplement the snippets as persistent eBPF programs (BCC or libbpf) that aggregate latencies in histograms for Grafana/Prometheus export.

▌ I got "ERROR: tracepoint not found: io_uring:io_uring_submit_sqe"

• Explored
  └ Search TRACE_EVENT\(io_uring_submit_sqe
    Search submit in io_uring.h
    Read io_uring.h

> io_uring:io_uring_submit_sqe isn’t defined in the kernel sources. The tracepoint that fires right before a request is issued is named io_uring:io_uring_submit_req (include/trace/events/io_uring.h:364). Use the
  names from that header (or list them live) before wiring up your script.

  Find The Right Tracepoints

  - Inspect the header directly (io_uring/include/trace/events/io_uring.h) or run one of:
      - sudo bpftrace -lv 'tracepoint:io_uring:*'
      - sudo perf list 'io_uring:*'
      - sudo cat /sys/kernel/tracing/events/io_uring/ (subdir names match tracepoints)
  - Note that the available set depends on the running kernel; older versions may lack some of the newer events.

  Example bpftrace Using Existing Events

  sudo bpftrace -e '
  tracepoint:io_uring:io_uring_submit_req /pid == TARGET/ {
    printf("%d submit %p opcode=%s user_data=0x%llx flags=0x%llx\n",
           pid, args->req, str(args->op_str), args->user_data, args->flags);
  }
  tracepoint:io_uring:io_uring_complete /pid == TARGET/ {
    printf("%d complete %p res=%d cflags=0x%x user_data=0x%llx\n",
           pid, args->req, args->res, args->cflags, args->user_data);
  }'

  Replace TARGET with your program’s PID.

  If You Need Function Coverage
  Attach kprobes/kretprobes instead of tracepoints:

  sudo bpftrace -e '
  kprobe:io_read, kretprobe:io_read /pid == TARGET/ {
    printf("%d %s ret=%d\n", pid, probe, retval);
  }'

  This works even when tracepoints aren’t available, at the cost of relying on function names/stability.

  So, switch your script to io_uring:io_uring_submit_req (and any other actual tracepoint names you need) or fall back to kprobes.

▌ Can you trace the completion queue event from read? Eg: I issue a read command for a net socket by putting it on the submission queue and calling io_uring_enter, when and how is the completion added to the CQE?

> Inline Read Completion → CQE

  - io_uring_enter() grabs ctx->uring_lock and calls io_submit_sqes() to drain the SQ ring (io_uring/io_uring.c:3513). Each SQE is normalised by io_submit_sqe() and eventually queued via io_queue_sqe() with
  IO_URING_F_COMPLETE_DEFER set for inline submissions (io_uring/io_uring.c:2288, io_uring/io_uring.c:2067).
  - io_queue_sqe() invokes io_issue_sqe(), which dispatches to the opcode handler. For IORING_OP_READ that is io_read() (io_uring/io_uring.c:1839, io_uring/rw.c:1022).
  - When sock_read_iter() returns data, kiocb_done() stores the byte count in the in-flight request (io_req_set_res()), runs FS notifications, and recycles buffers, then returns IOU_COMPLETE so io_issue_sqe() knows
  the request finished inline (net/socket.c:1153, io_uring/rw.c:664).
  - Because IO_URING_F_COMPLETE_DEFER was present, io_issue_sqe() does not publish the CQE immediately; it queues the request on ctx->submit_state.compl_reqs via io_req_complete_defer() (io_uring/io_uring.c:1849,
  io_uring/io_uring.h:493).

  Publishing To The CQ Ring

  - After the SQE batch, io_submit_state_end() runs. It first handles any linked fallbacks, then calls io_submit_flush_completions(); seeing entries on compl_reqs, it drops into __io_submit_flush_completions()
  (io_uring/io_uring.c:2338, io_uring/io_uring.h:214, io_uring/io_uring.c:1542).
  - __io_submit_flush_completions() walks each queued request, grabs a free CQE slot with io_get_cqe() (which bumps ctx->cached_cq_tail) and copies req->cqe into the ring (io_uring/io_uring.h:236). If the ring is
  full it spills into the overflow list.
  - Still under ctx->uring_lock, it calls __io_cq_unlock_post(), which performs io_commit_cqring(ctx) — a release store of the new tail into the shared ring — and wakes waiters (io_uring/io_uring.c:593, io_uring/
  io_uring.h:378). Eventfd / poll notifications go out via io_commit_cqring_flush() (io_uring/io_uring.c:609).

  Cleanup After Publish

  - Once all completions are copied, io_free_batch_list() recycles the requests and clears compl_reqs. Any deferred “drain” work is requeued, and the submit path exits with the ring lock dropped (io_uring/
  io_uring.c:1520, io_uring/io_uring.c:1568).

  Other Paths To CQE

  - If the read had to restart (e.g., first poll arming or buffered retry), the opcode returns IOU_ISSUE_SKIP_COMPLETE. The task-work handler io_req_rw_complete() eventually sets the CQE and calls
  io_req_task_complete(), which queues the request on compl_reqs; the same flush logic above publishes it (io_uring/rw.c:575, io_uring/io_uring.c:1695).
  - If the request ran on io-wq, completion goes through io_req_complete_post(), but since network sockets are pollable this path is uncommon (io_uring/io_uring.c:1900).

  In short, for a successful socket read the CQE is materialised only when the submit side flushes ctx->submit_state.compl_reqs: io_req_set_res() stores the data, io_req_complete_defer() queues it, and
  __io_submit_flush_completions() copies it into the CQ ring and publishes the new tail so userspace can observe it.

▌ Is it fair to say that io_uring (without SQPOLL) isn't really async, but it appears async from the application's perspective? What I mean by that is: when the application calls io_uring_enter it is doing a
▌ syscall into the io_uring code which is then performing all of the syscalls from the submission queue ring and then (if applicable) finishing up any queued io work into the cq. The efficiency gains come from
▌ batching syscalls so that the program only makes one user -> system transition (if batched appropriately). But the syscalls are done "synchronously" in the sense that they will all be done in one batch by the
▌ enter call.

> Not quite. Even without SQPOLL, io_uring is still capable of doing fully asynchronous work; you just happen to be the thread that initiates the requests when you call io_uring_enter(). What happens next depends
  on whether a submission can complete immediately:

  - Fast path (non-blocking succeeds): io_issue_sqe() runs the op inline and, if it finishes right there, queues the CQE for later flush. Yes, in that case the work happened “synchronously” while you were inside
  the syscall.
  - We need to block/poll: The kernel doesn’t stall your task. It either arms async polling (io_arm_poll_handler()), punts the request to io-wq worker threads, or reschedules it via task_work. Your thread returns
  from io_uring_enter() while those operations finish elsewhere; only the CQE delivery is deferred.
  - Queued completions: Whether the IO completed inline or elsewhere, CQEs are batched and published at the end of io_uring_enter() (or by the SQPOLL thread / task_work in the other modes). That batching is one of
  the big wins: fewer user/kernel transitions, better cache locality, and less lock churn. But it’s orthogonal to whether the underlying IO was actually asynchronous.

  So the syscall is a submission trigger and completion-flush point, not the exclusive executor of the work. When the target file/socket can make progress without blocking, you’ll see the “looks synchronous”
  behaviour; otherwise io_uring behaves like the async engine it’s designed to be.

▌ For a network socket which path does that take? That would be fast path because it's non-blocking right? Can you describe more how data arrives and what "executes" to get the data into user memory?
▌
▌ My intuition: User application calls io_uring enter, it issues a read which is non-blocking so that file descriptor is watched by the kernel. When data arrives on the NIC it executes an interrupt which runs some
▌ code (by the kernel?), looks up the file descriptor associated to the data, and then figures out where to DMA the data arriving in the NIC buffers to. The DMA can be async from the kernel's perspective (it
▌ doesn't execute on the CPU but is a separate hardware unit) but when the DMA is complete it interrupts the kernel to tell it that. Then once all operations linked to the data arriving are done the kernel can mark
▌ the file descriptor as ready to read.
▌
▌ Please tell me if I'm wrong, where I'm wrong, and provide a correct flow of events.

• I’m outlining how io_uring handles socket reads on Linux, starting from non-blocking checks with IOCB_NOWAIT to asynchronous polling and eventual data copying. I'll cover NIC DMA receiving data into kernel
  buffers, TCP stack wakeups, how io_uring registers waiters, and how read requests get reissued once data arrives. I’m also noting that data copying happens from kernel skbs to user memory during read completion,
  with zero-copy as an optional optimization. The explanation will clarify why “fast path” non-blocking reads only apply when data is ready, detailing the full flow to help the user understand io_uring’s network
  receive mechanism.

> For sockets the io_uring path can be either inline or truly asynchronous; it depends entirely on whether the socket already has payload waiting in its receive queue when you issue the read.

  Submit side

  1. Your task calls io_uring_enter(); the kernel walks the SQ ring (io_submit_sqes()), obtains each SQE, and dispatches it with io_issue_sqe() (io_uring/io_uring.c:1839).
  2. For IORING_OP_READ (or READ_FIXED), io_read() prepares the kiocb and runs sock_read_iter() in non-blocking mode. It sets IOCB_NOWAIT, so the socket stack will fail with -EAGAIN rather than sleeping (io_uring/
  rw.c:907, net/socket.c:1153).
  3. If the socket already has bytes queued, tcp_recvmsg() (or the UDP equivalent) copies them into your user buffer immediately. The request finishes inline, and completion is queued for a later flush (io_uring/
  rw.c:664).
  4. If no data is there, the read returns -EAGAIN. io_queue_async() sees the file is pollable, so it arms async polling: io_arm_poll_handler() adds the request to the socket’s wait queue via sock_poll_wait() with
  the EPOLLIN mask (io_uring/io_uring.c:2042, io_uring/poll.c:541). The request is now dormant until the socket becomes readable.

  What happens when packets arrive

  - The NIC DMA engine writes fresh frames into the driver’s RX ring (kernel memory). An interrupt schedules the driver’s NAPI poll routine, which pulls packets off the ring and builds sk_buffs.
  - The network stack processes the skb (e.g., TCP state machine), then appends payload to the socket’s receive queue. At that point it calls sk_data_ready() → sock_def_readable(), which wakes every entity waiting
  on the socket’s wait queue (epoll, io_uring polls, blocking readers).
  - For the io_uring request, the wakeup lands in io_poll_wake(). That marks the request ready and schedules its task_work (io_uring/poll.c:313). No data is copied yet; the skb still lives in kernel memory.

  Re-driving the read

  - Later (either immediately in softirq context if allowed, or when your task next enters the kernel), io_poll_task_func() runs. It locks the ring, reissues the original read (io_req_task_submit() →
  io_queue_sqe()), and clears the poll wait.
  - This second call to sock_read_iter() now finds data in the socket queue and copies it into your supplied iovecs. For ordinary sockets the copy is CPU-driven (copy_to_iter) from skb to user virtual memory;
  there’s no direct NIC → user DMA unless you opt into the kernel’s zero-copy features.
  - Once bytes are copied, io_req_set_res() records the result and io_req_complete_defer() queues the CQE. When io_uring_enter() exits (or the next time the ring is flushed), __io_submit_flush_completions() pulls
  the request off the deferred list, takes a CQ slot with io_get_cqe(), copies the struct io_uring_cqe, and publishes the new tail with a release store so user space can observe it (io_uring/io_uring.c:1548).

  Key corrections to your intuition

  - Calling io_uring_enter() doesn’t make the socket “blocking” or execute another syscall; it just attempts the read once. If that attempt would block, the request is suspended and resumed later when the socket
  is readable.
  - The data path is: NIC DMA → kernel RX buffers → socket receive queue → (on read) CPU copy into user buffer. The NIC never DMA’s directly into userspace unless you use something like IO_URING_RECVSEND_ZC or
  SO_ZEROCOPY.
  - The “asynchronous” part for sockets is handled by the poll infrastructure plus task_work or io-wq workers; your submitting thread is free to continue as soon as io_uring_enter() returns, even though the data
  hasn’t arrived yet.

  So the fast path does exist (when data is already queued), but io_uring’s async machinery takes over automatically when the socket would otherwise block.
```

### Back to working 10/06 evening

[Commit](https://github.com/benhirsch24/iouring_bench/commit/c8135a39de7265c222b1712bd44cacd895e25dd4)

* Added multishot timeout (easy)
* Reworked submitting so that there's a configurable threshold for
    batch size for submit. Now it will submit_and_wait when we have no
    completions done in the past loop. The idea being you can busy loop as
    long as there is work to pick up (in completions) and add new work to
    submission queue. Once there is no work though then you want to submit
    whatever you've got and wait.

Next:

* Test on EC2 instances
* Re-work request management - have opcode info as top 32 bits, file descriptor as bottom 32 bits, then you can read and write on a connection (wow)
* Add pubsub system

For my next benchmark I wanted to try a pubsub system - but specifically real-time fanout pubsub. With one publisher but many subscribers I can imagine that the opportunity for batching is much higher: if the server had eg 100 subscribers to a topic and the batch size for submit was 64, then you simply elide 100 syscalls for write into 2 (the main batch and then the submit_and_wait call assuming these are the only connections). This can be useful for an LL-HLS system like I described where many subscribers are waiting for the next piece of video to be produced - a syscall per viewer can be made into fewer syscalls.

The API could be super simple:

* `/publish/<topic>` - publishes to topic. I'll have to add HTTP POST body parsing but that shouldn't be hard.
* `/subscribe/<topic>` - subscribes to topic. Any publishes should be written to the subscriber.

Nuances I can think of:

* This API feels pretty ephemeral as a system. But in a CDN you'd want to fetch the content if it's already been produced/cached. Replayability of content would be interesting to add.
* Load testing isn't as simple as vegeta or wrk. Probably would want to write something simple in Go or Rust that has a configurable number of subscribers per topic and how often each publisher publishes. Also the size of the publish payload should be ~1MB like I'm testing now, but we could vary the sizes. This feels vibe-codeable.

### [10/7 Refactoring, NetDevConf presentations](#abstraction)

Just doing some refactoring after the first day of our IVS onsite

Thinking about how to abstract a little bit more. I think it'd be nice, but I think it'd be hard. For example if I add more and more routes, it would be difficult to have all this logic in "serve".

Ideally I probably want to pull the IoUring logic out into its own module, then share that with Request, and let Request add things to the submission queue. Then the main loop is just pulling from my deque of events, pushing to io_uring sq, pulling from cq, and then poking requests to keep going.

It would be fun to write an async framework around this, but I also know that's plenty fraught for reasons https://without.boats/blog/io-uring/

Oh I read some very interesting slides from NetDevConf 0x19 about two different zero-copy mechanisms (APIs?): https://netdevconf.info/0x19/docs/netdev-0x19-paper13-talk-slides/TheBattleOfTheZCs.pdf

It's io_uring vs devmem. It seems like devmem basically means that the kernel processes TCP packet headers (presumably to continue running the TCP state machine with the kernel code instead of re-creating a TCP stack elsewhere) and then transferring payloads directly from the NIC to device memory (ie GPU memory). These slides compare it to io_uring which has a zero-copy mechanism for a pool of buffers that are registered with the kernel. In this talk they're using udmabufs, which appear to be buffers memory mapped so data can be directly DMA'd to them. Not sure how io_uring zc competes with devmem other than reducing syscalls, I'll have to read more about it. Seems like there's a Google Summer of Code about it: https://blog.llvm.org/posts/2025-08-04-gpu-io-uring/

Reading more of these netdevconf presentations in general is interesting, here's one about [busy-polling to reduce latency](https://netdevconf.info/0x18/docs/netdev-0x18-paper10-talk-slides/Real%20world%20tips,%20tricks,%20and%20notes%20of%20using%20epoll-based%20busy%20polling%20v2.pdf). And I was just thinking through how IRQs work and learning more (/re-learning, I did take networking and OSes in college but it's been a while since I've had to think on this level) about the Linux networking stack, so great timing. There's actually a ton of good resources when you start looking for them.

**Next**: Probably a skosh more abstraction, then implement publish and subscribe as I talked about before.

I think I want to do what I discussed above: have my own Uring abstraction where Requests can own a reference to it and then submit their own entries to the (internal) submission queue. Then the main loop just drives everything from there.

### [10/10 More abstraction](#more-abstraction)

Refactored the implementation completely. My goal was to separate out any notion of Requests from the main event loop. I also didn't want to pass references to the Uring around to different users, so I went with a thread-local variable so that Connections could call `uring::submit(...)` from anywhere. That also makes the event loop simple as it doesn't have to think about pushing entries into its backlog.

There's 3 main commits:

[Move the event loop into its own file](https://github.com/benhirsch24/iouring_bench/commit/8969bcd8c46ce1e3bd1a1bf4adc4f9804a986318) - This just uses an anonymous function to create requests. I didn't like how I had to use a request trait and then Box it. This is also where I started with thread-local variables.

[Submit from anywhere](https://github.com/benhirsch24/iouring_bench/commit/29a792c0905c8bfba98ac0c1613856bf192e994a) - This commit changes from a RefCell to an UnsafeCell. The reason behind this is that RefCell will panic if you try to borrow the RefCell when there's a mutable reference to it already borrowed, which would be implied by how this works. The call graph would look like:

```
// main.rs
uring.run() // <-- Mutable borrow here

// uring.rs
loop {
  handle_submissions();
  for each completion cqe:
    handle_completion(cqe);
}

// connections.rs
handle_completion(cqe):
  // Do things to get new submission events sqe
  uring.submit(sqe); // <-- Second mutable borrow here
```

While this is IMO safe because it's a single-threaded application (uring will never read the submission queue backlog while the handler is adding new things to it), RefCell is not happy. I wouldn't want to do `Arc<Mutex<...>>` because there's no reason for a lock in a single-threaded application. So I went with UnsafeCell. [Here's a little conversation with Claude to get to this](https://claude.ai/share/5e9fae8c-e9b5-4fdb-b291-00039ad5e404).

Instead of

```
thread_local! {
   static URING: RefCell<Option<Uring>> = RefCell::new(None);
}

pub fn submit(sqe) {
  URING.with(|uring| {
    let mut uring_ref = uring.borrow_mut();
    match uring_ref {
      Some(u) => u.submit(sqe),
      None => panic!("aaa")
    }
  });
}
```

We have:

```
thread_local! {
   static URING: UnsafeCell<Option<Uring>> = UnsafeCell::new(None);
}

pub fn submit(sqe) {
  URING.with(|uring| {
    unsafe {
      let mut uring_ref = &mut *uring.get();
      match uring_ref {
        Some(u) => u.submit(sqe),
        None => panic!("aaa")
      }
    }
  });
}
```

This worked! But my main gripe was that my uring code still had to be Request-aware. So for my last refactor I basically pulled all of the completion handling logic into a `handler` function which could be used from `uring::run(handler)`. I also got statistics working again through a `uring::stats()` function. [Commit](https://github.com/benhirsch24/iouring_bench/commit/0dd2223c25e645e77e75deb691f3d8eb9a26fbd2)

Now main looks like (Rust pseudo-code):

```
uring::init(args);
let accept = create_multishot_accept();
let timeout = create_multishot_timeout();
uring::submit([accept, timeout]);
let mut connections = HashMap::new();
uring::run(move |result, user_data, flags| {
  let op = user_data.op()?;
  match op {
    Accept => {
      let conn = Connection::new();
      conn.handle(op, user_data, flags);
      connections.insert(result, conn); // Note - Accept returns the new fd in result. All other opcodes the fd is packed in user_data
    },
    Recv => {
      let mut conn = connections.get_mut(&user_data.fd);
      conn.handle(op, user_data, flags);
    },
    Send => {
      let mut conn = connections.get_mut(&user_data.fd);
      conn.handle(op, user_data, flags);
    },
    Timeout => {
      let stats = uring::stats()?;
      info!("Stats: {}", stats);
    },
  }
});
```

I'm still semi-unhappy with this as I don't think the user of a library should have to define uses for opcodes like this... ideally maybe I could have `uring::set_timeout(|| do_timeout())` and then `uring::set_reader(...)`? I'll have to noodle on it.

Or maybe I have a Server type which wraps the uring::run() call and then I provide something to Server? The most ideal of course would be an async runtime which is simplest, but of course runs into weirdness when you drop/cancel Futures.

Anyways, my goal is not to make the perfectly useable library here, it's to benchmark shit and make lots of packets go.

### [10/11 Writing Pubsub Server](#pubsub-server)

Let's get to making a PUB/SUB type system. First there's the protocol. I want to have it be super simple:

1. Subscribe to channel
2. Publish to channel

That's it for now. Sketch of connection handling:

1. Accept
2. Read first bytes off the wire. If it starts with HTTP -> go into HTTP handling. If it starts with PUBLISH -> Publish handling
3. If it's a subscriber I need a way to add each subscriber connection to the publisher connection's interest list
4. If there's another read and the connection is a PUBLISHER skip the protocol parsing, just forward to subscribers
5. On read 0 bytes => if it's a publisher, then we need to notify each subscriber of closing and close them up
    6. How would I know if a subscriber times out? If I write and get ECONNRESET then they've left.
    6. Or I could have subscribers send an explicit BYE and then close the socket from my end. That's probably most reasonable.

I could also add HTTP/1.1 Keep Alive handling as well, but later.

The ultimate test of my "library" is if I could write a subscriber and publisher client with the library. That would be pretty sick.

I also just realized I have no way of identifying closed sockets and removing them from the connection map. Gotta start a TODO list.

[Added a "PUBSUB.md" file.](https://github.com/benhirsch24/iouring_bench/blob/main/PUBSUB.md)

#### Worklog stream of thoughts on pubsub implementation

Parsing was pretty easy. I'm realizing that writing is going to be the hard bit.

In my HTTP implementation I just have a single buffer in the Connection which gets a response set and then the send implementation is geard towards sending it all out.

Now for pubsub I start to bifurcate. I want to send an `OK\r\n` as the response from each. That can just use the response buffer.

Then for every publish message I'm going to queue up a send for every subscriber. That message needs to be allocated and live until every send has completed. I'll need to track every send to each subscriber and wait for the completion and associate it to the buffer.

I might want to abstract this out, I can have a refcounted buffer and I can add this info in the userdata.

Current plan:

* Create channel map and buffer pool in main, pass to all connections.
* Channel map is a map of channel (String) to list of file descriptors which are the subscribers
* On publish it parses the message, looks up the channel in the map, creates a buffer in the buffer pool with the count of subscribers, then issues a send per subscriber
* On send completion event handle/serve will see that the fd corresponds to a subscriber, advance the write buffer for that fd in the buffer in the buffer pool, then sends more if necessary
* If send says that it's written all the bytes for that subscriber then it decrements the refcount
* If the refcount is 0 then we release the buffer back into the pool

I'll probably not start with an actual buffer pool (yet) but just allocate a buffer on each write. But I can go towards there to reduce allocations.

**End of night**

Whew. This is actually a good bit of work to get it all plumbed through. I've got publish message parsing working, I started to plumb through a signal so that if a client sends a BYE the handler can signal to close the connection. I'm sure I'm doing a lot mechanically that I wouldn't have to do in a real framework. If I had a working async library I'd do this:

```
loop {
  let stream = listener.accept()?;
  spawn(handle_conn(stream));
}

fn handle_conn(stream) {
    let line = stream.read_line().await?;
    if line == "PUBLISH" {
        handle_publish(stream)
    }
}

fn handle_publish(stream) {
    let futures = vec![];
    while let Some(line) = stream.read_line().await? {
        for subscriber in get_subscribers(self.channel) {
            // Need to spawn as I don't want to block waiting for each send to complete
            let fut = spawn(move || subscriber.send(line).await?);
            futures.push(fut);
        }
    }
    wait_on_all(futures);
}
```

*Something* like that. It's certainly a little simpler than writing complex state handling code in a struct. Will have to keep noodling on the ergonomics of it.

This is all for fun anyways right?

**8:22PM**

Have a working implementation, although I need to get the carriage returns from the publishes over to the subscribers.

Part of this is that within `Connection::read_line` I'm converting it to a String which is easy, but not a) efficient or b) what I really want which is just to copy bytes over.

I think there's a better way to do this with BytesMut and Bytes, I need to get there. I could probably do something with `self.read_buffer.split_off`. Or I wonder if `bytes::Buf` could be useful? Something to look into.

Bytes was actually very easy. I should go through and replace read buffer with it.

I should also try to move every read and write over to the BufferPool. That would probably get me to a really good place for when I want to do kernel buffers. Then I'd check a buffer out of the pool for a read, check it back in when I'm done, and same with sends.

### [Cool test: Can I make a pubsub load test with my own library?](#dogfooding)

`Nc` is annoying me because it won't always close the socket for some reason [which is apparently a known issue](https://stackoverflow.com/questions/53634911/why-does-sending-d-with-netcat-not-trigger-an-eof-when-reading-from-a-unix-sock).

I want to test that my pubsub implementation works. So to do that I want to:

1. Create a publisher
2. Create two subscribers
3. Publish a message
4. Verify both subscribers got it
5. Have one subscriber leave
6. Verify (by server logs) that the subscriber left
7. Publish another message
8. Verify (by server logs) that the last subscriber got it
9. Last subscriber leaves
10. Publish a message
11. Verify (by server logs) that no message was sent.

As I start to try to write this, I think a generic buffer pool implementation would be super useful actually. Rather than each connection managing its own state.

It's also just so confusing to write a state machine like this. Publisher is like:

1. Enqueue opcode to send "PUBLISH ch"
2. On returned send enqueue opcode to read "OK"
3. On returned read enqueue opcode to send "hello"
4. On returned send close

Would so much rather write

```
conn.send("PUBLISH ch\r\n");
conn.read(&mut buf);
// check buf == OK
conn.send("hello\r\n");
conn.close()
```

Anyways, I actually got the publisher working pretty easily. I think the pattern of a Connection trait with `handle` as the only function and then a connections map is pretty simple. I shouldn't really even need all the shit I have in uring::run(...) in main right now, I should abstract it all away through a Server type that wraps uring::run. Something like that.

Got a very simple send and receive working. I think the next thing would be to coordinate between publisher and subscriber to do what I was talking about above.

### Popping up one level

Ok so why did I go down this path: I want to see if I can get better performance gains for io_uring if I have a workload that can bundle many syscalls in one submission. One way I can test that out is with a fan-out pubsub system with many subscribers to few publishers. I should try to get to a point where I can test this theory as soon as possible.

### [10/12 PubSub client with Callbacks](#callbacks)

It's nice that this toy program works, but how to scale it up? Ideally I could parameterize and say N publishers with M subscribers per publisher.

The other thing I was thinking about last night was extracting the pubsub server from my src/main.rs and then making this an actual library crate and having a src/bin/http_server.rs and src/bin/pubsub_server.rs. It would probably be much cleaner.

Also this state machine handling gets very complex. I was thinking I could set up a callback system where I register callbacks to unique completion event IDs. Considering that the user data is a u64 I could register a callback into a global callback thingy and then start writing callbacks. EG:

```
use std::collections::HashMap;

fn main() {
    let mut map: HashMap<u64, Box<dyn FnOnce()>> = HashMap::new();
    map.insert(0_u64, Box::new(|| println!("Hello")));
    map.insert(1_u64, Box::new(|| println!("World")));
    map.remove(&0_u64).unwrap()();
    map.remove(&1_u64).unwrap()();
}
```

This works. At that point I'm getting close to a full on async runtime which I've been skirting around this whole time... but I don't think I have the mental space to tackle that yet lol.

Ideally, even if I didn't have an async system, I'd want some way for the publisher to know that all subscribers have connected and that the publisher can begin publishing. Then I'd want a way for the subscribers to notify that they've all received the message before going to the next one. It's nearly impossible to do that without some sort of global state system and references between publisher and subscriber.

Or I could run the uring in one thread and have a coordinating thread that watches the state. Maybe that's easiest for now. Quick sketch:

```
let publisher = Arc::new(Mutex::new(Publisher::new()));
let (tx, rx) = std::sync::mpsc::channel();
std::thread::spawn(move || {
    // Wait on message
    rx.recv().unwrap();
    publisher.lock().unwrap().send_message().expect(...);
    rx.recv().unwrap();
});

let messages = RefCell::new(subscribers.len());
uring::run(...);

fn Subscriber::recv_ack {
    if let Some(l) = read_line()? {
        *messages.borrow_mut() -= 1
        if *messages.borrow() == 0 {
            tx.send(true);
        }
    }
}
```

Something like that. Each subscriber has a reference to a refcell around an integer with the number of subscribers. When the recv completion event says it received the full line (one message) it decrements it, and then the last one signals the producing thread to poke the publisher to send another message. But wait, since I have thread local storage... will the publisher reference (behind the arc mutex) in the second thread not have access to the uring that exists in thread 0? Oof. Ok never mind, but I'll leave this bit here anyways for posterity.

This brings me back to my callback system. How long would that take? I guess I could try it, since the uring run function doesn't have any sort of opcode logic in it I think I could easily experiment with this idea in a program.

**Callback implementation**

I did a very simple callback registry like this:

```
use std::rc::Rc;
use std::cell::RefCell;
use std::collections::HashMap;

struct CallbackRegistryInner {
    map: HashMap<u64, Box<dyn FnOnce(i32) -> anyhow::Result<ConnectionResult>>>,
    counter: u64,
}

impl CallbackRegistryInner {
    fn new() -> Self {
        Self {
            map: HashMap::new(),
            counter: 0,
        }
    }

    fn add_callback<T>(&mut self, f: Box<T>) -> u64
    where
        T: FnOnce(i32) -> anyhow::Result<ConnectionResult> + 'static,
    {
        let c = self.counter;
        self.map.insert(c, f);
        self.counter += 1;
        c
    }

    fn call_back(&mut self, id: u64, res: i32) -> anyhow::Result<ConnectionResult> {
        let cb = self.map.remove(&id);
        match cb {
            Some(cb) => cb(res),
            None => anyhow::bail!("No callback registered for {id} res={res}")
        }
    }
}

#[derive(Clone)]
struct CallbackRegistry {
    inner: Rc<RefCell<CallbackRegistryInner>>,
}

impl CallbackRegistry {
    fn new() -> Self {
        Self {
            inner: Rc::new(RefCell::new(CallbackRegistryInner::new())),
        }
    }

    fn add_callback<T>(&self, f: Box<T>) -> u64
    where
        T: FnOnce(i32) -> anyhow::Result<ConnectionResult> + 'static,
    {
        self.inner.borrow_mut().add_callback(f)
    }

    fn call_back(&self, id: u64, res: i32) -> anyhow::Result<ConnectionResult> {
        self.inner.borrow_mut().call_back(id, res)
    }
}
```

Which worked great for exactly one callback. But then I started to run into issues when I wanted to nest callbacks within callbacks and do it from a Publisher function because it referenced `self` which does not have lifetime `'static`.

I [consulted with Claude a little bit](https://claude.ai/share/a37b9bf9-8086-4f94-a7cf-b429db2269e1) to confirm my thinking, but I think I could get away with wrapping Publisher up in an `Rc<RefCell<>>` and doing an Inner pattern here to clone and pass that into the closure.

I got a ["You're absolutely right!"](https://absolutelyright.lol/). I personally don't mind it that much though tbh. Yes, Claude was wrong probably because I was already talking about the Rc RefCell pattern with thread local storage. But the thing is, I can just... compile it, check if it's working or not, go back to Claude (if I choose) and then keep going. If Claude doesn't give me a perfect answer first try that's just how it goes.

**3PM** after a walk. Got this all wired up and working. Had to use UnsafeCell for the callback registry because of course it's being borrowed and used at the same time. But we are going to assume that everything is safely single threaded ;)

After this is all done it's pretty nice! It's not async and it's definitely callback hell, but it's pretty nice. The code ends up looking like this:

```
fn send_hi(&mut self) -> anyhow::Result<()> {
    let send_e = {
        let mut inner = self.inner.borrow_mut();
        inner.send_buffer.put_slice(b"PUBLISH ch\r\n");
        let ptr = inner.send_buffer.as_ptr();
        let to_send = inner.send_buffer.len();
        opcode::Send::new(types::Fd(inner.fd), ptr, to_send.try_into().unwrap())
    };
    let ud = add_callback({
        let inner = self.inner.clone();
        Box::new(move |res| {
            // TODO: What if send is bigger than res?
            info!("Send completed! res={res}");
            let read_e = {
                let mut inner = inner.borrow_mut();
                let ptr = inner.read_buffer.as_mut_ptr();
                opcode::Recv::new(types::Fd(inner.fd), ptr, (inner.read_buffer.capacity() - inner.read_buffer.len()) as u32)
            };
            let ud = add_callback({
                let inner = inner.clone();
                Box::new(move |res: i32| {
                    let send_e = {
                        let mut inner = inner.borrow_mut();
                        info!("Received res={res}");
                        let newlen: usize = inner.read_buffer.len() + (res as usize);
                        unsafe {
                            inner.read_buffer.set_len(newlen)
                        };
                        let line = inner.read_line().expect("There should be one read by now");
                        if line != "OK\r\n" {
                            anyhow::bail!("Expected OK");
                        }
                        // Split removes anything that's been filled already. Put our message in
                        // to send instead.
                        let _ = inner.send_buffer.split();
                        inner.send_buffer.put_slice(b"hello\r\n");
                        let ptr = inner.send_buffer.as_ptr();
                        let to_send = inner.send_buffer.len();
                        opcode::Send::new(types::Fd(inner.fd), ptr, to_send.try_into().unwrap())
                    };
                    let ud = add_callback({
                        let inner = inner.clone();
                        Box::new(move |_res| {
                            info!("Done!");
                            inner.borrow_mut().conn.shutdown(std::net::Shutdown::Both)?;
                            Ok(())
                        })
                    });
                    uring::submit(send_e.build().user_data(ud.into()))?;
                    Ok(())
                })
            });
            let e = read_e.build().user_data(ud).into();
            uring::submit(e)?;
            Ok(())
        })
    });
    uring::submit(send_e.build().user_data(ud))?;
    info!("Sending hi ud={ud}");
    Ok(())
}
```

As I'm looking at this, I could probably do away with the Publisher and PublisherInner stuff and instead just abstract out the Bytes/BytesMut stuff to its own buffer pool. I'll get there. Also could make the entries simpler. For now, I just want to make it publish -> subscribe.

I went down this path for the subscribers and it was really easy.

```
// Spawn 5 subscribers
let num_subscribers = 5;
let dones = Rc::new(RefCell::new(num_subscribers));
for _ in 0..num_subscribers {
    let dones = dones.clone();
    let subscriber = TcpStream::connect("127.0.0.1:8080")?;
    info!("Sending subscribe");
    let mut send = BytesMut::with_capacity(1024);
    send.put_slice(b"SUBSCRIBE ch\r\n");
    let ptr = send.as_ptr();
    let n = send.len();
    let op = opcode::Send::new(types::Fd(subscriber.as_raw_fd()), ptr, n.try_into().unwrap());
    let ud = add_callback(move |res| {
        // Sending SUBSCRIBE completion
        trace!("Sent subscribe res={res}");
        let _ = send.split();
        let mut ok = BytesMut::with_capacity(1024);
        let ptr = ok.as_mut_ptr();
        let op = opcode::Recv::new(types::Fd(subscriber.as_raw_fd()), ptr, ok.capacity() as u32);
        let ud = add_callback(move |res| {
            // Expected OK receive completion
            trace!("Recv res={res}");
            unsafe { ok.set_len(res.try_into()?) };
            let line = read_line(ok).expect("There should be one read by now");
            if line != "OK\r\n" {
                anyhow::bail!("Expected OK");
            }

            let mut read = BytesMut::with_capacity(1024);
            let ptr = read.as_mut_ptr();
            let op = opcode::Recv::new(types::Fd(subscriber.as_raw_fd()), ptr, read.capacity() as u32);
            let ud = add_callback(move |res| {
                // Expected message1 completion
                if res < 0 {
                    anyhow::bail!("Got negative return from receive: {res} fd={}", subscriber.as_raw_fd());
                }
                trace!("Received message res={res}");
                unsafe { read.set_len(res.try_into()?) };
                let line = read_line(read).expect("There should be one message");
                let msg = std::str::from_utf8(line.as_ref())?;
                info!("Subscriber received message {msg}");
                *dones.borrow_mut() -= 1;
                if *dones.borrow() == 0 {
                    uring::exit();
                }
                Ok(())
            });
            uring::submit(op.build().user_data(ud))?;
            Ok(())
        });
        uring::submit(op.build().user_data(ud))?;
        Ok(())
    });
    let e = op.build().user_data(ud);
    uring::submit(e)?;
}
```

I added a `uring::exit()` function to make this work (which now helps cargo no longer complain at me).

Like I said, callback hell but it's getting there. I think one way I could simplify this is to wrap TcpStream in a UringTcpStream and then I can implement `send(buf, || {...})` and `recv`. Like I said before, then I'm halfway to building an async runtime.

I think I'm going to wrap this worklog up here and post it because it's getting quite long. Then worklog 3 should be about benchmarking this against a Tokio or Glommio implementation.

It would also be cool to write a blog post about the journey of this from the complicated state machines (confusing), to callbacks (also confusing, but nicer), to async (complicated in implementation but even nicer).
