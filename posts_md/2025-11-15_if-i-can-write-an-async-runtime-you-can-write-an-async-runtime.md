---
Date: 2025-11-15
---

# If I can write an async runtime, you can write an async runtime

Now I'm just a simple country Principal Engineer at AWS, but I truly believe that if I can write an async runtime in Rust then you can write an async runtime in Rust.

I've been working on my personal learning project [twoio](https://github.com/benhirsch24/twoio/tree/main) for a couple of months now. I started by wanting to learn about `io_uring` but as that evolved I re-learned all of the lessons of event loop driven IO and the motivations behind async Rust (or runtimes like Go has). In this post I'd like to discuss an intersection of how to write an async runtime, how `io_uring` works (a bit), how to implement some common IO constructs, and also how to implement an actual program.

First some caveats:

This async runtime is not meant to be production ready. Use tokio, use glommio, use something else. This is purely a personal project.

This is meant to be single threaded, not multi-threaded. This let me write lock-free code and keep it simple. I'm not going to go over work stealing from other threads or other fun topics.

I have not even attempted to solve tough `io_uring` problems like what happens when you drop a Future or cancel a Future and the buffer the kernel has been told to write to is freed.

The goal of this article and my project is to learn some concepts and be able to go one level deeper in the future if you need to.

The way I'm going to structure this page is in the first half I will go through writing an async runtime as I did for twoio. In the second half I will document in more detail the code I wrote for the underlying file/network operations. In the third half I'll cover a couple of illustrative programs. And in the fourth half I'll go into the process I went through writing `io_uring` programs and why I ended up writing an async runtime in the first place.

Halves 3 and 4 will be a separate post because this got way too long.

## Async runtime

### What is IO Uring?

First let's talk about [io_uring](https://man7.org/linux/man-pages/man7/io_uring.7.html). This is a new(ish) Linux API for asynchronous IO. The basic idea is that your user space application has two shared ring buffers with the kernel: a queue for submitting IO operations for the kernel to do, and a queue for the kernel telling your userspace program which operations have completed (with their results). The goal is to reduce the number of expensive syscalls your program makes; instead, you make one syscall to the kernel which consumes all of the IO operations you want to do off the submission queue in one go.

Your core IO loop then is structured like this (Rust pseudocode):

```rust
fn run(
  to_submit: Vec<Entry>,
  handle_completion: Fn(result: u32, user_data: u32, to_submit: &mut Vec<Entry>),
  completions_done: Fn(to_submit: Rc<RefCell<Vec<Entry>>>)
) {
  let uring = Uring::new();
  let submission_queue, completion_queue = uring.queues();
  loop {
    // Process all of the done IO operations
    for completion in completion_queue.entries() {
      handle_completion(completion.result(), completion.user_data(), &to_submit);
    }
    completions_done();

    // Push any new IO operations generated into the shared submission queue
    for entry in to_submit {
      submission_queue.push(entry);
    }
    // Submit this batch to the kernel and wait for at least 1 completion event
    uring.submit_and_wait(1);
  }
}
```

Hopefully this is fairly self explanatory, but our basic run loop is pretty simple. You put one or more `Entry`s describing your IO onto the submission queue and then make one syscall to the kernel telling it to grab all of the operations off the queue. At some point in the future the IO is done and `Entry`s representing the result are pushed onto the completion queue which you consume and do something with. That something may generate more IO operations to do, which can be put on the `to_submit` queue, and so on.

Entries look like this using the popular [io-uring](https://docs.rs/io-uring/latest/io_uring/squeue/struct.Entry.html) crate. You can attach a u64 user data to it to tag the operation submitted to the completion event.

```rust
let read_entry = opcode::Read::new(file_descriptor, buffer_pointer, length).build().user_data(next_user_data_tag());
```

When the completion event finishes

#### What's the alternative?

First let me link [this excellent post](https://without.boats/blog/why-async-rust/) which I'll essentially re-hash in a different post.

If you're unfamiliar with the motivations behind async/await and event loops I'll discuss that more in the next post, but very quickly let's go over epoll.  Epoll looks pretty similar to the above but with some differences.

Again, very much pseudocode:

```rust
fn read_callback(fd: RawFd) -> Result<EpollResult> {
  let mut buffer = [0u8; 64];
  // Perform syscall
  let res = read(fd, &mut buffer, 64);
  // Error handling
  // ...
  Ok(EpollResult::Continue)
}

fn run(
  start: Fn(epoll: &mut Epoll),
  handle_ready_fd: Fn(fd: RawFd),
) {
  let socket = listener_socket();
  let epoll = epoll_create();
  // This adds IO at the beginning, like creating a listening socket
  start(&mut epoll);
  let mut epoll_events = [epoll_event_default(); 64];
  loop {
    let num_events = epoll_wait(&mut epoll_events);
    for i in 0..num_events {
        let fd = epoll_events[i].fd;
        match handle_ready_fd(fd) {
            Ok(EpollResult::Delete) => {
                epoll.remove(fd);
            },
            _ => {
                // Keep going, error handling, this is pseudo code
            }
        }
    }
  }
}
```

Here we have an epoll container and file descriptors get registered into the container telling the kernel we're interested in knowing when they're "ready" for data to be read or something similar. Then once there are some file descriptors ready we do something very similar where we can call a callback for each file descriptor.

**The crucial difference** is that the callback is what does the syscall to actually read the data. In the `io_uring` example the data was placed in the read buffer that was submitted. In epoll the kernel says "this fd is ready to read" and you now can read from it. If you have thousands of open connections that have bytes ready to read that means doing thousands of syscalls whereas `io_uring` enables you to do just one.

### Basic runtime around io uring

Let's start with a basic runtime around `io_uring`. We know from above that each operation can have its own user data u64 and each operation results in a i32 result. We need a way to register that a new operation is going to happen and a way to get the result once it's complete. We can assume we only need to get the operation once and can remove it afterwards.  We can also assume that the executor owns the uring as well.

```rust
pub struct Executor {
  next_op_id: u64,
  op_to_result: HashMap<u64, i32>,
}

impl Executor {
  pub fn new() -> Self {
    Self {
      uring: IoUring::new(1024).expect("io uring new"),
      next_op_id: 0,
      op_to_result: HashMap::new(),
    }
  }

  pub fn get_next_op_id(&mut self) -> u64 {
    let op = self.next_op_id;
    self.next_op_id += 1;
    op
  }

  pub fn get_result_for_op(&mut self, op: u64) -> Option<i32> {
    self.op_to_result.remove(op)
  }

  pub fn handle_completion(&mut self, op: u64, res: i32) {
    self.op_to_result.insert(op, res);
  }
}
```

So we submit an operation and at some point in the future the result becomes available. We need something that wraps this concept up: a [Future](https://doc.rust-lang.org/std/future/trait.Future.html)!

Futures look like this:

```rust
pub trait Future {
    type Output;

    // Required method
    fn poll(self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<Self::Output>;
}

pub enum Poll<T> {
  Ready(T),
  Pending
}
```

When we submit an operation we return some type that implements a Future. Whenever you poll the Future it is either still Pending (meaning the corresponding completion event hasn't come in on the completion queue yet) or it's Ready when it has, in which case we return that value. Let's write an example Future around File::open.

```rust
struct OpenFuture {
  op_id: u64,
  _path: CString,
  done: bool,
}

fn open<P: AsRef<Path>>(path: P) -> OpenFuture {
  let op_id = executor.get_next_op_id();

  // Turn the path into a "C String" (ASCII characters) and get a pointer to that.
  let path_bytes = path.as_ref().as_os_str().as_bytes();
  let c_path = CString::new(path_bytes)
      .map_err(|_| std::io::Error::other("paths containing NUL bytes are not supported"))?;

  // Create the opcode with the same arguments you would use for `openat`
  let opcode = opcode::OpenAt::new(types::Fd(libc::AT_FDCWD), c_path.as_ptr())
      .flags((libc::O_RDWR | libc::O_CLOEXEC) as _)
      .mode(0);

  // Add this operation to the queue which will be submitted in a batch to the uring
  // when the runtime decides to flush the events and call submit_and_wait
  let submission_entry = opcode.build().user_data(opcode);
  uring::submit(submission_entry);

  // Return a future representing the operation that will complete in the future
  OpenFuture {
    op_id,
    _path: c_path,
    done: false,
  }
}

impl Future for OpenFuture {
  type Output = std::io::Result<twoio::File>;
  fn poll(self: Pin<&mut Self>, _cx: &mut Context<'_>) -> Poll<Self::Output> {
    let me = self.as_ref();
    if me.done {
      panic!("Don't poll me again, I'm done");
    }

    match executor::get_result_for_op(op_id) {
      Some(res) => {
        me.done = true;
        // There will be no errno set because with a batch of IO completions there would be no way to know which errno it corresponded to.
        // Instead if the result is negative then that corresponds to the error code
        if res < 0 {
          Poll::Ready(Err(std::io::Error::from_raw_os_error(-res)))
        } else {
          // Returns the file.
          Poll::Ready(Ok(twoio::File::new(res)))
        }
      },
      None => Poll::Pending,
    }
  }
}
```

Ok, there's a lot in here.

**Opening the file**: When we open a file we need to pass the string representing the name of the file. There is a mjaor `io_uring` subtlety which is that the bytes representing the file name must be allocated where the pointer you put in the opcode is until the completion event returns. Remember, that pointer is sent to the kernel asynchronously and you don't know when the kernel will look at it to open the file so we store it in the OpenFuture.

**The OpenFuture**: This is pretty straighforward to me. Every time someone calls `poll` on the future it asks the `Executor` if there's a result for the operation it was created with. If so, the Future is now `Ready` and it returns the file. If not that means the operation hasn't completed yet and so the Future is `Pending`.

As I noted in the comments, another subtlety of `io_uring` is how it handles errors. Since you may have multiple IO operations complete at the same time, checking errno doesn't really make sense. Which syscall did it correspond to? Instead the result value returned will be negative and the (negated) value is the error code. So if the first argument to `openat` was a bad file descriptor then the completion event would return `-9` for `EBADF`.

Great, we have some simple Future and we can imagine how to write new futures in The Future. How do we actually use these things in a program which makes sense?

#### Implementation Detail: Thread local variables

A note on implementation: You'll see that I use `executor::spawn(..)` or `uring::submit(..)` instead of `executor.spawn` or `uring.submit`. This is because I've stuck those behind a thread-local static variable like so:

```rust
thread_local! {
  static URING: UnsafeCell<Option<Uring>> = const { UnsafeCell::new(None) };
}

pub fn init() {
  URING.with(|u| unsafe {
    let u = &mut *u.get();
    if u.is_some() {
      return;
    }
    let new_u = Uring::new();
    *u = Some(new_u);
  });
}

pub fn submit(sqe: squeue::Entry) {
  URING.with(|u| unsafe {
    let u = &mut *u.get();
    match u.as_mut() {
      Some(u) => u.submit(sqe),
      None => panic!("uring not initialized")
    }
  });
}
```

Because each thread will have one uring and one executor and only one task executes at a time this is safe to do. If we use RefCell then you run into the issue of the RefCell being borrowed by uring during `run` and then again borrowed by uring during `handle_completion` or something similar. UnsafeCell gets around this and it's safe because everything runs on a single thread.

I repeated this pattern for executor as well for spawn and things like getting the current task ID for logging.

### Tasks and Futures

How do we want to write programs? In my opinion, if we're writing programs that deal with IO then we inherently want to write straightforward imperative code.

Let's say we want to open a file and read from it. We should express this as:

```rust
use twoio::fs::File;
async fn read_from_file(path: PathBuf) -> std::io::Result<()> {
  let f = File::open(path).await?;
  let mut buf = [0u8; 1024];
  let _n = f.read(&mut buf, 1024).await?;
  println!("Read: {}", std::str::from_utf8(&buf)?);
  f.close().await?;
}
```

First you open a file. Then you read from it into a buffer. Then you print that data. Then you close it. It looks just like how you would write a synchronous program, but your function is denoted `async` and you have to `await` every operation.

Why do we need to do this? Imagine we have two files we want to read at the same time and these files are on some network-mounted file system where there is latency between saying `f.read(...)` and having that data available.

```rust
async fn main() {
  // Do file1.txt in The Background
  let task1 = spawn(read_from_file("file1.txt").await.unwrap());
  // Do file2.txt in The Background
  let task2 = spawn(read_from_file("file2.txt").await.unwrap());
  wait_for_both_tasks(task1, task2).await;
  // Do more stuff
}
```

If we want to work on both of these files at the same time on one thread we need to be able to interleave these tasks on the same thread.

1. Open file1.txt
2. Open file2.txt
3. file1.txt is now opened, start reading from file1.txt
4. file2.txt is now opened, start reading from file2.txt
5. Read from file1.txt is ready, close file1.txt
6. Read from file2.txt is ready, close file2.txt
7. file1.txt closed
8. file2.txt closed

How do we do this? What I find helpful is to first focus on an `async fn`

#### What is an async fn?

Let's look at the simplest possible async function:

```rust
async fn zero() -> i32 {
  0
}
```

Through the magic of the Rust compiler this function will be transformed into something that implements `Future<i32>`. If we use our `OpenFuture` from above we could have something like:

```rust
async fn open_and_zero() -> i32 {
  let _f = twoio::fs::File::open("path.txt").await.unwrap();
  0
}
```

We open a file (creating a Future and awaiting it) and then return 0. The Rust compiler creates a state machine for you like (pseudocode):

```rust
struct AnonymousFuture {
  state: State,
  // variables/bindings here
}

enum State {
  State0,
  State1,
}

impl Future for AnonymousFuture1 {
  fn poll(...) -> Self::Output {
    loop {
        match self.state {
          State::State0 => {
            self._temp_file = twoio::fs::File::open("path.txt");
            match self._temp_file.poll(...) {
                Poll::Ready(v) => {
                    self.state = State1;
                    self._open_return = v;
                    continue;
                },
                Poll::Pending => return Poll::Pending;

            }
          },
          State::State1 => {
            self._open_return.unwrap();
            return Ready(0);
          },
        }
    }
  }
}
```

This is pseudocode and I'm cobbling my understanding together from various Rust threads. If you have any corrections on this or good reading, please feel free to contact me and I will update this article.

The basic idea is that an async function gets transformed into a state machine that implements Future. The anonymous struct needs to capture all of the variables that your async function creates so that they live across await points; if those values were allocated on the stack and then you returned from the function call these intermediate variables would no longer be alive. Therefore all the code that you wrote gets moved into `poll` in one of these states and gets executed when the anonymous Future gets polled. Once the function makes it to the end it returns `Ready` with the final return value.

#### Running multiple async functions concurrently

Let's now think again about how we want to write programs. To handle multiple things concurrently on one thread (tens of thousands of open network connections for example) we need to store and poll many many Futures. There is some top-level Future per "task", for example a single top level Future handles a single HTTP request. That async function will have a bunch of code that executes, possibly reading from and writing to the network socket multiple times in the course of its life. Every time we poll the function it returns `Poll::Pending` until finally it returns `Poll::Ready`.  We don't want to call `poll` on every single Future repeatedly: instead since `io_uring` sends us a completion event whenever some IO is done, we can associate the task to the operation it's waiting for.

Let's write some pseudocode that we want to be able to write for a server:

```rust
let listener = TcpListener::bind("0.0.0.0:8080").await.unwrap();
loop {
  let conn = listener.accept().await.unwrap();
  executor::spawn(async move {
    let buffer = vec![0u8; 1024];
    conn.read(&mut buffer);
    // do some stuff with buffer
    let response = "HTTP/1.1 200 OK\r\n";
    conn.write_all(&response).await.unwrap();
  });
}
```

This is a pretty simple server loop. A TCP listener is created and accept is called; at some point a new connection comes in and we create a new routine which is run in the background (from the perspective of the accept loop) which handles that new connection. Spawn returns immediately (from the perspective of the accept loop) and we do it again. Each spawned function reads from the socket, writes to the socket, and then exits.

Now let's think through the sequence of events in our Executor and how this would work:

1. There is some Task created for the accept loop code.
2. The executor polls the accept loop task (task 0)
3. An Accept future is created which submits an `opcode::Accept` to `io_uring` and then returns Pending
4. A connection comes in and `io_uring` returns a completion event. That fires the executor `handle_completion` callback which associates the user data tag to the result. It also tells the executor that task 0 is ready for progress. The Executor puts task 0 in the ready queue.
5. The executor iterates through the ready queue and re-polls task 0 which is at the accept future
6. Now the Accept future finds the result in the Executor's hashmap. That result is a new socket. Task 0 now calls `executor::spawn` with an async function (which is a Future). This creates task 1. `spawn` immediately places task 1 in the ready queue.
7. Task 0 loops and goes back to accept
8. Executor finds task 1 in the ready queue and polls it. Task 1 allocates a Vector, creates a read future, tells the executor that the operation id for the read future associates to task 1, and then returns Pending.
9. A new connection comes in and we repeat step 4 creating task 2.
10. Task 2 is polled, we repeat step 8 to create the read future for task 2
11. Data comes in for task 2, the `handle_completion` callback is called which wakes up task 2 (note that task 1 has not gotten data yet so it is still waiting).
12. Life goes on in this fashion for a while, etc

Hopefully this illustrates what is going on underneath the hood. We spawn Tasks to the executor (a Task is just a way of saying a Future that is an independent unit for the executor to keep polling) and the executor polls these Tasks once they're ready. The Task code in the poll function creates other Futures which submit their operations to `io_uring`, associates the task ids to the operation ids, and returns Pending until they are woken up.

From the Executor perspective we need to add a few things:

1. Get a new task ID
2. Associate an operation ID to a task ID
3. Wake up tasks once an operation completion comes in
4. Handle the tasks which are now ready to be re-polled

```rust
struct Executor {
  // ... other fields we defined above
  tasks: HashMap<u64, LocalBoxFuture<'a, ()>>,
  op_to_task: HashMap<u64, u64>,
  next_task_id: u64,
  ready_queue: Vec<u64>,
}

impl Executor {
  // ... other functions we defined above

  fn get_next_task_id(&mut self) -> u64 {
    let tid = self.next_task_id;
    self.next_task_id += 1;
    tid
  }

  fn associate_op_to_task(&mut self, op_id: u64, task_id: u64) {
    self.op_to_task.insert(op_id, task_id);
  }

  fn spawn(&mut self, fut: impl Future<Output = ()> + 'static) {
    let task_id = self.get_next_task_id();
    self.tasks.insert(task_id, fut.boxed_local());
    self.ready_queue.push(task_id);
  }

  fn handle_completion(&mut self, op_id: u64, res: i32) {
    self.op_to_result.insert(op, res);
    match self.op_to_task.get(&op) {
      Some(tid) => self.ready_queue.push(tid),
      None => panic!("We aren't going to implement cancellation");
    }
  }

  // This function handles the ready queue
  fn completions_done(&mut self) {
    loop {
      if self.ready_queue.len() == 0 {
        return;
      }

      let ready_queue = std::mem::take(&mut self.ready_queue);
      for task_id in ready_queue.iter() {
        if let Some(task) = self.tasks.remove(&task_id) {
          let mut ctx = Context::from_waker(Waker::noop());
          match task.as_mut().poll(&mut ctx) {
            Poll::Ready(_) => {
              // Nothing to do, it's ready and we haven't implemented JoinHandles
            },
            Poll::Pending => {
              self.tasks.insert(*task_id, task);
            },
          }
        }
      }
    }
  }
}
```

Hopefully this code is pretty straightforward and self explanatory.

Spawn takes a future, puts it in the tasks map, and immediately pushes it to the ready queue to be ran (polled) for the first time. I'm using `futures::future::LocalBoxFuture` which is a type alias to `Pin<Box<dyn Future<...>>>`. I don't want to cover Pin in any great detail here, but I'll add some references at the bottom.

I've filled in that the `completions_done` handler which is called once all completion events are processed by `io_uring` handles all of the ready tasks. We loop through the `ready_queue` until there's no tasks remaining - we do this because polling a task may generate new work that needs to be immediately serviced. Otherwise it will have to wait until `io_uring` returns with completions again. A more smarter design would add time slicing and co-operative yielding here so the ready queue handling doesn't loop too many times, but again: not production, personal learning project.

At this point we have a real functioning async runtime! Believe me, I also was surprised how easy this was once you dig into it.

This concludes the first part of this page, introducing how simple it can be to write an async runtime. Next I'll go into writing common constructs like Files and TCP Streams so that we can actually do something with it.

## Implementing IO and other useful things

Now we can think about implementing IO (and other useful things).

### Files

Let's start with the File example. What do people do with files? Open them, read from them, write to them, and close them. Seek too, but I haven't implemented that yet.

We already opened a file above. Let's move on to reading from a file. The [futures crate](https://docs.rs/futures) provides the trait `AsyncRead` to describe reading from a resource.

```rust
pub trait AsyncRead {
  fn poll_read(self: Pin<&mut Self>, cx: &mut Context<'_>, buf: &mut [u8]) -> Poll<Result<usize, Error>>;
}
```

If you wanted to use this you could

```rust
let file = File::open("path.txt").await?;
let mut buffer = vec![0u8; 1024];
let read_future = file.read(&mut buffer);
loop {
  match read_future.poll_read(Context::from_waker(Waker::noop())) {
    Poll::Ready(n) => println!("Read {n} bytes"),
    Poll::Pending => println!("Waiting on read"),
  }
}
```

Hmm... this kinda sucks. I want to read as if I was using synchronous Rust's [read](https://doc.rust-lang.org/std/io/trait.Read.html#tymethod.read) method. Luckily `futures` provides the [AsyncReadExt trait](https://docs.rs/futures/latest/futures/io/trait.AsyncReadExt.html).

```rust
pub trait AsyncReadExt: AsyncRead {
    fn read<'a>(&'a mut self, buf: &'a mut [u8]) -> Read<'a, Self>
       where Self: Unpin { ... }
}
```

And `read` is actually a provided method. This means that if you implement `poll_read` for your type you get `read` for free!

`read` returns a [Read](https://docs.rs/futures/latest/futures/io/struct.Read.html) type which [implements poll](https://docs.rs/futures/latest/futures/io/struct.Read.html#method.poll). This means that you can `.await` it like any other Future instead of writing a loop! All it really boils down to is that the [Read::poll function calls file.poll_read](https://docs.rs/futures-util/0.3.31/src/futures_util/io/read.rs.html#23) but that's all it needs to do for you to write nicer code.

Getting back to our File implementation:

```rust
impl AsyncRead for File {
    // TODO: This implementation is not accurate as the buffer could be modified when we return
    // Poll::Pending. Really we need to implement an internal buffer. But if you are careful you
    // can use it.
    fn poll_read(
        mut self: Pin<&mut Self>,
        _cx: &mut Context<'_>,
        buf: &mut [u8],
    ) -> Poll<Result<usize, futures::io::Error>> {
        let mut me = self.as_mut();
        let fd = me.fd;

        // Check if there's an outstanding operation. If so check in the executor if there's a result yet.
        if let Some(op_id) = me.read_op_id {
            return match executor::get_result(op_id) {
                Some(res) => {
                    me.read_op_id = None;
                    if res < 0 {
                        Poll::Ready(Err(std::io::Error::from_raw_os_error(-res)))
                    } else {
                        Poll::Ready(Ok(res as usize))
                    }
                }
                None => Poll::Pending,
            };
        }

        // No operation yet, so schedule the operation with uring and tell the executor about it
        let op_id = executor::get_next_op_id();
        me.read_op_id = Some(op_id);
        let ptr = buf.as_mut_ptr();
        let len = buf.len() as u32;
        let op = opcode::Read::new(types::Fd(fd), ptr, len).offset(CURRENT_POSITION);
        executor::associate_op_to_task(op_id, false);
        match uring::submit(op.build().user_data(op_id)) {
            Ok(_) => Poll::Pending,
            Err(e) => {
                me.read_op_id = None;
                Poll::Ready(Err(Error::other(format!("Uring problem: {e}"))))
            }
        }
    }
}
```

There's a lot here, but also not too much. Ignoring the first check, let's go to the bottom block. We basically create a `Read` opcode on the file's file descriptor and build it with a pointer to the buffer. We tell the executor about it and submit it to uring. We also set this operation's ID on the file.

Winding back, we only want one outstanding read operation on the File at a time for now. This makes getting the result for the operation simpler. If we wanted to allow for multiple reads to the file to be queued up at the same time we could allow Files to be copied or create new Files from the file descriptor; this is actually a nice advantage for `io_uring` because it could make this simpler. But for now, one read per File object at a time.

If the operation exists then we look in the executor to see if there's a result. If there is a result we parse it: if negative we can use `std::io::Error::from_raw_os_error(-res)` to return a nicely typed error; otherwise, return the result number of bytes read.

And that's it! Write and Close are implemented similarly.

#### Safety

**NOTE** as I mentioned in the comments, this is not safe. Think about this code:

```rust
let mut buf = vec![0u8; 1024];
let read = file.read(&mut buf);
drop(buf);
```

On creating the Read future we issue a Read opcode to the Linux kernel and give it a raw pointer to the buffer's data. Creating the Future submits the opcode to the uring, But we don't await the future and drop the buffer. However without removing that opcode from the submission queue before it's submitted or without submitting an [AsyncCancel](https://docs.rs/io-uring/latest/io_uring/opcode/struct.AsyncCancel.html) the kernel will try to read data into where that pointer pointed to, but that location is no longer valid.

There's a [great post by boats on this exact topic](https://without.boats/blog/io-uring/) so I won't try to add more to the discussion, but bring up some thoughts for the reader. Do you submit a cancel when the future is dropped? Do you have to wait for that cancel to go through (ie: blocking)? Do you move buffers inside the type and use [AsyncBufRead](https://docs.rs/futures/latest/futures/io/trait.AsyncBufRead.html) (which is what is meant to be done)? There's a lot of nuance to this problem.

Luckily I get to throw up my hands and say "don't fuck up" because this is a library I'm doing for fun, not for production.

### Network

Next let's talk networking which is the domain I spend my time in as a video streaming engineer at Amazon IVS (the video backend behind Twitch).

As you can imagine reading and writing to TCP sockets is very much like it is for Files. What is interesting and novel is the idea of "multishot" opcodes. For example [AcceptMulti](https://docs.rs/io-uring/latest/io_uring/opcode/struct.AcceptMulti.html)

Normally, as illustrated above, you write code like this for a server:

```rust
loop {
  let conn = listener.accept().await?;
  spawn(async move {
    // do stuff with conn
  });
}
```

Every call to `accept` does a syscall to the kernel waiting for a connection to be handshooken and put in the accept queue. Instead in `io_uring` you can use a "multishot" opcode to submit accept once and keep it armed until you decide to unarm it.

However that means that we need to adapt the Executor to the paradigm where operations won't be immediately deleted from the operation to results map. The TcpListener also needs to know if a multishot accept has already been submitted so it doesn't re-submit one. Luckily both are fairly easy.

```rust
struct Executor {
  // ...
  // This is a vector of results in case multiple results have come in
  multi_results: HashMap<u64, Vec<i32>>,
  // op to task needs to know if the operation is a multi operation
  op_to_task: HashMap<u64, (u64, bool)>,
}

fn associate_op_to_task(&mut self, op_id: u64, is_multi: bool) {
  let task_id = self.get_current_task_id();
  self.op_to_task.insert(op, (task_id, is_multi));
}

fn get_result_for_op(&mut self, op: u64) -> Option<i32> {
  let (_, is_multi) = self.op_to_task.get(&op).unwrap();
  if *is_multi {
    if let Some(v) = self.multi_results.get_mut(&op) {
      v.pop()
    } else {
      None
    }
  } else {
    self.op_to_result.remove(op)
  }
}

fn handle_completion(&mut self, op_id: u64, res: i32) {
  let (task_id, is_multi) = self.op_to_task.get(&op_id);
  if is_multi {
    self.multi_results.entry(op).or_default().push(res);
  } else {
    self.results.insert(op, res);
  }
}
```

Basically we maintain another map of operations to results, but this time the results are a Vec in case eg a bunch of connections come in before we can accept them all.

Now we can implement `accept_multi`. Let's assume that TcpListener has been created and bound to an address already so we have a file descriptor. That all should be pretty straightforward, or you can [look at my code](https://github.com/benhirsch24/twoio/blob/6a6a9c0318c2abc993325671484e8090b2671497/src/net.rs#L22). I did not make that async since I figure it typically gets called once at the beginning of a binary.

```rust
struct TcpListener {
  fd: RawFd,
  accept_multi_op: Option<u64>,
}

impl TcpListener {
  pub fn accept_multi(&mut self) -> AcceptMultiFuture {
    if let Some(op_id) = self.accept_multi_op.as_ref() {
      return AcceptMultiFuture{ op_id: *op_id };
    }

    let op = opcode::AcceptMulti::new(types::Fd(fd));
    let op_id = executor::get_next_op_id();
    self.accept_multi_op = Some(op_id);
    executor::associate_op_to_task(op_id, true);
    uring::submit(op.build().user_data(op_id));
    AcceptMultiFuture{ op_id }
  }
}

struct AcceptMultiFuture {
  op_id: u64,
}

impl Future for AcceptMultiFuture {
  type Output = std::io::Result<twoio::TcpStream>;
  fn poll(self: Pin<&mut Self>, _cx: &mut Context<'_>) -> Poll<Self::Output> {
    let me = self.as_ref();
    match executor::get_result(self.op_id) {
      Some(res) => {
        if res < 0 {
          Poll::Ready(Err(std::io::Error::from_raw_os_error(-res)))
        } else {
          Poll::Ready(Ok(twoio::TcpStream::from_raw_fd(res)))
        }
      },
      None => Pending,
    }
  }
}
```

And that's really it. Every time TcpListener creates an AcceptFuture for the user it checks if there's an outstanding operation; if not it submits it, if so it just makes a future with that op id. When AcceptFuture is polled it'll check the Vector in executor for that operation id and return sockets (unless there's an error) or Pending.

You can imagine that [connect](https://github.com/benhirsch24/twoio/blob/6a6a9c0318c2abc993325671484e8090b2671497/src/net.rs#L148), [read](https://github.com/benhirsch24/twoio/blob/6a6a9c0318c2abc993325671484e8090b2671497/src/net.rs#L196), and [write](https://github.com/benhirsch24/twoio/blob/6a6a9c0318c2abc993325671484e8090b2671497/src/net.rs#L248) are fairly straightforward, and here are links to code if you want to read them.

### Other Things

Here's a collection of other things I need to write concurrent programs: timeouts, channels, and wait groups. Is it obvious that I program in Go as a day job?

I don't think I need to go into detail about timeouts, [here's the code](https://github.com/benhirsch24/twoio/blob/main/src/timeout.rs#L19). I think it's cool that Timeouts also have a multishot option for ticker-like behavior. It's occurring to me that I should probably call that variant a `Ticker` instead of timeout in my library.

Channels and WaitGroups are interesting because they need to interact with the Executor without being driven by `io_uring` completion events.

#### Channels

Much has been written about Go's channels and for good reason: it makes sharing data between otherwise independent routines very simple.

For the program that I was writing I implemented a [multi-producer single consumer channel](https://github.com/benhirsch24/twoio/blob/6a6a9c0318c2abc993325671484e8090b2671497/src/sync/mpsc.rs#L81). I wanted multiple writers to send data to a single reader who would read each message in turn and do something with it.

This boils down to having a single shared double-ended queue (`std::collections::VecDeque`) shared between senders and the receiver.

This is what `Rc<RefCell<...>>` is for. `Rc` is a ref counter wrapper which allows multiple locations in code to have a reference to the same data which is allocated on the heap. Once the last holder of a reference drops it the data will automatically be freed. `RefCell` allows mutability of this data by the multiple borrowers by shifting the borrow checking rules to runtime instead of compile time.

The whole implementation is pretty simple:

```rust
pub enum SendError {
    Closed,
    Full, // We're unbounded right now but maybe one day
}

pub struct Receiver<T: Clone> {
    inner: Inner<T>,
}

impl<T: Clone> Receiver<T> {
    pub fn recv(&mut self) -> RecvFuture<T> {
        *self.inner.receiver_task_id.borrow_mut() = Some(executor::get_task_id());
        RecvFuture {
            inner: self.inner.clone(),
        }
    }

    pub fn close(&mut self) {
        *self.inner.closed.borrow_mut() = true;
    }
}

pub struct RecvFuture<T: Clone> {
    inner: Inner<T>,
}

impl<T: Clone> Future for RecvFuture<T> {
    type Output = Option<T>;
    fn poll(self: Pin<&mut Self>, _cx: &mut Context<'_>) -> Poll<Self::Output> {
        let me = self.as_ref();

        if *self.inner.closed.borrow() && self.inner.data.borrow().is_empty() {
            return Poll::Ready(None);
        }

        if me.inner.data.borrow().is_empty() {
            return Poll::Pending;
        }

        Poll::Ready(Some(me.inner.data.borrow_mut().pop_front().unwrap()))
    }
}

#[derive(Clone)]
pub struct Sender<T: Clone> {
    inner: Inner<T>,
}

impl<T: Clone> Sender<T> {
    pub fn send(&mut self, t: T) -> Result<(), SendError> {
        if *self.inner.closed.borrow() {
            return Err(SendError::Closed);
        }

        self.inner.data.borrow_mut().push_back(t);
        if let Some(task_id) = *self.inner.receiver_task_id.borrow() {
            executor::wake(task_id);
        }
        Ok(())
    }
}

#[derive(Clone)]
struct Inner<T>
where
    T: Clone,
{
    data: Rc<RefCell<VecDeque<T>>>,
    receiver_task_id: Rc<RefCell<Option<u64>>>,
    closed: Rc<RefCell<bool>>,
}

pub fn channel<T: Clone>() -> (Receiver<T>, Sender<T>) {
    let inner = Inner {
        data: Rc::new(RefCell::new(VecDeque::new())),
        receiver_task_id: Rc::new(RefCell::new(None)),
        closed: Rc::new(RefCell::new(false)),
    };
    (
        Receiver {
            inner: inner.clone(),
        },
        Sender {
            inner: inner.clone(),
        },
    )
}
```

As I said, there's a single shared ref counted double-ended queue that messages get placed in and consumed from.

Since it's unbounded I did not make `send` async. Senders can place as many messages in the queue as they want and blow their memory up. Making this bounded wouldn't be too complicated, but you'd have to decide whether to error if the queue was full or make send async and then block the caller until there's space. Or make a `try_send` function for the first case. Anyways.

`recv` is async, so we create a `RecvFuture` when calling that. When that future is polled it checks the queue to see if there's an item; if so it returns it, otherwise it returns Pending. Nicely, this is cancellation safe.

You can use this like:

```rust
let (rx, tx) = twoio::mpsc::channel();
executor::spawn(async move {
  loop {
    match rx.recv().await {
      Some(v) => println!("v"),
      None => return,
    }
  }
});

executor::spawn({
  let tx = tx.clone();
  async move {
    tx.send(1);
  }
});

executor::spawn({
  let tx = tx.clone();
  async move {
    tx.send(2);
  }
});

executor::run();
```

Wow, it's the lamest program ever created. Two routines send a number and a third routine receives and prints them. This will block forever, [view this example for a better example](https://github.com/benhirsch24/twoio/blob/main/examples/mpsc.rs).

#### WaitGroups

I use WaitGroups in Go all the time. I think to be more "Rust-y" I should have created a JoinHandle on a call to `executor::spawn` and those JoinHandles could have been awaited or `join!`ed or something, but I implemented WaitGroups.

WaitGroup is like a refcounted condition variable. You create a wait group and then get Guards from it. Some Task waits on the WaitGroup while other Tasks execute asynchronously, and that first task wants to know when all the other tasks have finished before proceeding. Those other tasks take ownership of the Guards and when the Guard is dropped that first task needs to be woken up to see if it can progress.

```rust
#[derive(Clone, Default)]
struct WgInner {
    waiters: Rc<RefCell<u64>>,
    task_id: Rc<Cell<Option<u64>>>,
}

#[derive(Default)]
pub struct WaitGroup {
    inner: WgInner,
}

impl WaitGroup {
    pub fn add(&mut self) -> Guard {
        let op_id = executor::get_next_op_id();
        *self.inner.waiters.borrow_mut() += 1;
        executor::schedule_completion(op_id, false);
        Guard {
            inner: self.inner.clone(),
        }
    }

    pub fn wait(&self) -> WaitFuture {
        self.inner.task_id.set(Some(executor::get_task_id()));
        WaitFuture {
            inner: self.inner.clone(),
        }
    }
}

pub struct WaitFuture {
    inner: WgInner,
}

impl Future for WaitFuture {
    type Output = ();

    fn poll(self: Pin<&mut Self>, _cx: &mut Context<'_>) -> Poll<Self::Output> {
        let me = self.as_ref();
        if *me.inner.waiters.borrow() == 0 {
            log::trace!("WaitGroup done");
            return Poll::Ready(());
        }

        log::trace!("WaitGroup poll num_waiters={}", me.inner.waiters.borrow());
        Poll::Pending
    }
}

pub struct Guard {
    inner: WgInner,
}

impl Drop for Guard {
    fn drop(&mut self) {
        *self.inner.waiters.borrow_mut() -= 1;
        if let Some(task_id) = self.inner.task_id.get() {
            log::trace!("Guard being dropped task_id={task_id}");
            executor::wake(task_id);
        }
    }
}
```

I hope this is fairly self explanatory. The `waiters` field is the ref count - if the WaitFuture (which shares the single inner value of the WaitGroup) is waited on and the count is positive then it will return Pending, or if it's 0 it's ready. The Guards need to know which Task is waiting on that WaitFuture and when the Guard is dropped it wakes that task up to see if it can make progress.

You could probably have the Guard only wake the main task when the count is 0, but that operation is pretty cheap IMO.
