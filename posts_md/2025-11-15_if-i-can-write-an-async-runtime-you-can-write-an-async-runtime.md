---
Date: 2025-11-15
---

# If I can write an async runtime, you can write an async runtime

Now I'm just a simple country Principal Engineer at AWS, but I truly believe that if I can write an async runtime in Rust then you can write an async runtime in Rust.

I've been working on my personal project [twoio](https://github.com/benhirsch24/twoio/tree/main) for a couple of months now. I started by wanting to learn about `io_uring` but as that evolved I re-learned all of the lessons of event loop driven IO and the motivations behind async Rust (or runtimes like Go has). In this post I'd like to discuss an intersection of how to write an async runtime, how `io_uring` works (a bit), how to implement some common IO constructs, and also how to implement an actual program.

First some caveats:

This async runtime is not meant to be production ready. Use [tokio](https://docs.rs/tokio/latest/tokio/), use [glommio](https://docs.rs/glommio/latest/glommio/), use [monoio](https://docs.rs/monoio/latest/monoio/), use something else. This is purely a personal project.

This is meant to be single threaded, not multi-threaded. At best this will scale to a thread-per-core design. This let me write lock-free code and keep it simple. I'm not going to go over how to write a work stealing scheduler or anything more advanced.

I'm not going to cover various features of `io_uring` like SQPOLL or kernel registered files or anything like that.

This is not safe code. I have not even attempted to solve tough `io_uring` problems like what happens when you cancel a Future and the buffer the kernel has been told to write to is freed.

The goal of this article is to write down what I've done and learned, and hopefully to help whoever reads this article learn some similar lessons.

The way I'm going to structure this article is that in the first half I will go through writing an async runtime as I did for twoio. In the second half I will document in more detail the code I wrote for the underlying file/network operations as well as some useful concurrent programming constructs.

In a separate article I'll cover a couple of illustrative programs I wrote as well as the process I went through writing `io_uring` programs and why I ended up writing an async runtime in the first place.

## Async runtime

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

First you open a file. Then you read from it into a buffer. Then you print that data. Then you close it. It looks just like how you would write a synchronous program, but your function explicitly says that it is `async` and inside the function you have to add this `.await` to every IO call.

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

How do we do this? Let's start with how we tell the Linux kernel to perform IO.

### What is IO Uring?

First let's talk about [io_uring](https://man7.org/linux/man-pages/man7/io_uring.7.html). This is a new(ish) Linux API for asynchronous IO. The basic idea is that your user space application has two shared ring buffers with the kernel: a queue for userspace to submit IO operations to the kernel, and a second queue for the kernel to tell your userspace program which operations have completed. The goal is to reduce the number of expensive syscalls your program makes. Instead, you can make one syscall to the kernel with a batch of operations in one go.

Your core IO loop is structured like this (Rust pseudocode):

```rust
fn run(
  to_submit: Vec<Entry>,
  handle_completion: Fn(result: u32, user_data: u32, to_submit: &mut Vec<Entry>),
  completions_done: Fn(to_submit: Rc<RefCell<Vec<Entry>>>)
) {
  let uring = Uring::new();
  let submission_queue, completion_queue = uring.queues();
  loop {
    // Process all of the completed IO operations.
    // Each completion callback may generate more future IO operations
    for completion in completion_queue.entries() {
      handle_completion(completion.result(), completion.user_data(), &mut to_submit);
    }
    // Call a callback to inform that all completions are done on this loop iteration
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
let read_entry =
  opcode::Read::new(file_descriptor, buffer_pointer, length)
    .build()
    .user_data(next_user_data_tag());
```

This "entry" to the submission queue acts as a call to `read(2)`. The first parameter is the file descriptor to read from, the second param is a pointer to the buffer to read into, and the third is the length of the buffer. There is an extra parameter attached to the entry for user data which is used when the kernel returns the result of the operation to us. It helps our runtime associate the completion entry with the result of the read to the specific read (in case we have many reads in flight).

#### What's the alternative to io_uring?

First let me link [this excellent post](https://without.boats/blog/why-async-rust/) which I'll essentially re-hash in the next post.

If you're unfamiliar with the motivations behind async/await and event loops I'll discuss that more in the next post, but very quickly let's go over epoll. An epoll program looks pretty similar to the above but with some differences.

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
                // Keep going... do error handling... this is pseudo code...
            }
        }
    }
  }
}
```

File descriptors get registered into an epoll "container" to tell the kernel we're interested in knowing when they're "ready" for an operation. Once there are some read file descriptors we do that "something" like call `read(2)`.

The crucial difference is that the once the file is ready our program performs a syscall for every ready file. If you have thousands of open connections that have bytes ready to read that means doing thousands of syscalls whereas `io_uring` enables you to do just one transition from userspace to the kernel (and a fairly lightweight one at that).

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

From the application's point of view we want to submit an operation and then read the result at some point in the future the result becomes available. We need something that wraps this concept up: a [Future](https://doc.rust-lang.org/std/future/trait.Future.html)!

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

When we submit an operation we return some type that implements a Future. Whenever you poll the Future it is either still `Pending` (meaning the corresponding completion event hasn't come in on the completion queue yet) or it's `Ready` when it has, in which case we return that value.

Let's write an example Future for opening a file. We can assume the open operation has been submitted so all the `OpenFuture` needs to do is return `Ready` with the open file descriptor once the completion entry arrives.

```rust
struct OpenFuture {
  op_id: u64,
  _path: CString,
  done: bool,
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

Pretty straighforward. Every time someone calls `poll` on the future it asks the `Executor` if there's a result for the operation it was created with. If so, the Future is now `Ready` and it returns the file. If not that means the operation hasn't completed yet and so the Future is `Pending`.

Now we can create the `OpenFuture` with our own `File::open`:

```rust
impl File {
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
}
```

When we open a file we need to pass the string representing the name of the file. That string must be allocated and cannot be freed until the completion entry returns which is why we store it in the OpenFuture struct. Remember, that pointer is sent to the kernel asynchronously and you don't know when the kernel will look at it to open the file.

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

We've got a lot of building blocks now. Let's revisit our example program from up above and think about how we will actually run this with the executor.

```rust
use twoio::fs::File;
async fn read_from_file(path: PathBuf) -> std::io::Result<()> {
  let f = File::open(path).await?;
  let mut buf = [0u8; 1024];
  let _n = f.read(&mut buf, 1024).await?;
  println!("Read: {}", std::str::from_utf8(&buf)?);
  f.close().await?;
}

fn main() {
  let handle = spawn(read_from_file("file1.txt").await.unwrap());
  wait_for_handle(handle);
}
```

We have this async function that we need to give to the executor which will then run it somehow until it's done. So what exactly is an `async fn`?

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

We open a file (creating a Future and awaiting it) and then return 0. The Rust compiler creates a state machine for you like this (pseudocode):

```rust
struct OpenAndZeroFuture {
  state: State,
  // variables/bindings here
}

enum State {
  State0,
  State1,
}

impl Future for OpenAndZeroFuture {
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
            Poll::Pending => Poll::Pending,
          }
        },
        State::State1 => {
          self._open_return.unwrap();
          Poll::Ready(0)
        },
      }
    }
  }
}
```

This is pseudocode and I'm cobbling my understanding together from various Rust threads. If you have any corrections on this or good reading, please feel free to contact me and I will update this article.

The basic idea is that an async function gets transformed into a state machine that implements Future. All of the variables that you create are just fields in the struct. If you await a future (in this example there's a temporary value created by the call to `File::open(..)` then the struct needs to save that value to poll it again in the future.

As your executor successively calls `poll(..)` on the `Future` created by your `async fn` it will progress through the different states until it returns `Poll::Ready` with the result of the function (in this case it's just `0`).

#### Running multiple async functions concurrently

What we need to write in the executor is a way to call `poll(..)` on the Futures we create. These Futures that are polled are scheduled initially by the call to `executor::spawn()`. The Futures that are scheduled by `spawn` we call a `Task` because they represent an independent task to execute.

These Tasks may, in the course of their execution, create and poll various other Futures. This means the executor needs to store all of the Tasks, whether they're ready to be polled or not, and also know when to call `poll` on Tasks that are ready to be polled. Otherwise with 10s of thousands of tasks we may end up iterating over and polling every single task on every single run.

From the section on [the basic IO Uring runtime above](#basic-runtime-around-io-uring) we know that `io_uring` sends us a completion event whenever some IO is done. This means that we can associate the task to the operation it's waiting for.

From the `Executor` perspective we need to add a few things:

1. Accept tasks and give them some ID (`spawn`)
2. Associate a submitted operation ID to a task ID
3. Wake up a task when the completion callback is called
4. Poll every task that's ready to be polled

Let's write some code.

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

  fn spawn(&mut self, fut: impl Future<Output = ()> + 'static) {
    let task_id = self.get_next_task_id();
    self.tasks.insert(task_id, fut.boxed_local());
    self.ready_queue.push(task_id);
  }

  fn get_next_task_id(&mut self) -> u64 {
    let tid = self.next_task_id;
    self.next_task_id += 1;
    tid
  }

  fn associate_op_to_task(&mut self, op_id: u64, task_id: u64) {
    self.op_to_task.insert(op_id, task_id);
  }

  fn handle_completion(&mut self, op_id: u64, res: i32) {
    self.op_to_result.insert(op, res);
    match self.op_to_task.get(&op) {
      Some(tid) => self.ready_queue.push(tid),
      None => panic!("We aren't going to implement cancellation");
    }
  }

  // This function polls all the tasks that are ready to be polled
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

The `completions_done` function (which is called once all completion events are processed by `io_uring`) loops through the `ready_queue` until there's no tasks remaining; we do this instead of going through the ready queue once because polling a task may generate new work that needs to be immediately polled. Otherwise it will have to wait until `io_uring` returns with completions again. A more smarter design would add time slicing and co-operative yielding here so the ready queue handling doesn't loop too many times, but again: not production, personal learning project.

At this point we have a real functioning async runtime! Believe me, I also was surprised how easy this was once you dig into it.

#### Walk through an example

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

Hopefully this illustrates what is going on underneath the hood. We spawn Tasks to the executor (a Task is just a way of saying a Future that is an independent unit for the executor to keep polling) and the executor polls these Tasks once they're ready, and it knows they're ready from the callback our uring loop calls. The Task code in the poll function creates other Futures which submit their operations to `io_uring`, associates the task ids to the operation ids, and returns `Pending` until they are woken up.


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

#### JoinHandles

JoinHandles are deceptively simple. The point of a JoinHandle is that you want to `await` a `Future` you `spawn` and get the result back from it.

```rust
struct JoinHandle<T> {
  ...
}

impl<T> Future for JoinHandle<T> {
  type Output = T;
  // poll implementation
}

async fn do_something() {
  let join_handle = executor::spawn(async {
    0
  });
  let result = join_handle.await;
  println!("{result}"); // should print 0
}
```

I didn't want to tackle this at first because I wasn't sure if I had to do fancy type magic with `Executor::spawn`. How can we have `spawn` take Futures with different return types? Turns out you don't have to! You can use the `spawn` function we already have and depend on the Rust `async fn` compilation machinery.

All `Executor::spawn` really cares about is that *tasks* return `()`. Futures used within `async fn`s can return values all they want.

```rust
pub struct JoinHandle<T> {
    ret: Rc<RefCell<Option<T>>>,
    task_id: Rc<Cell<Option<u64>>>,
}

pub fn spawn<T: 'static>(fut: impl Future<Output = T> + 'static) -> JoinHandle<T> {
    EXECUTOR.with(|exe| unsafe {
        let exe = &mut *exe.get();
        let ret = Rc::new(RefCell::new(None));
        let task_id = Rc::new(Cell::new(None));
        let jh = JoinHandle {
            ret: ret.clone(),
            task_id: task_id.clone(),
        };
        exe.as_mut().unwrap().spawn(async move {
            let t = fut.await;
            *ret.borrow_mut() = Some(t);
            if let Some(tid) = task_id.get() {
                wake(tid);
            }
        });
        jh
    })
}
```

JoinHandle needs two things: the value to be returned once the spawned `Future` returns, and the ID of the task to wake once the future is ready. The `spawn` function (module level `spawn`, not `Executor::spawn`) creates an `async fn` which awaits the `Future` we want to run and then simply places the value it returns in the `Rc<RefCell<T>>` in our `JoinFuture`.

The `Future` implementation is then pretty straightforward:

```rust
impl<T> Future for JoinHandle<T> {
    type Output = T;
    fn poll(self: Pin<&mut Self>, _cx: &mut Context<'_>) -> Poll<Self::Output> {
        let me = self.as_ref();
        schedule_completion(get_next_op_id(), false);
        me.task_id.set(Some(get_task_id()));
        let mut r = me.ret.borrow_mut();
        if r.is_none() {
            return Poll::Pending;
        }
        let t = r.take().unwrap();
        Poll::Ready(t)
    }
}
```

When the JoinHandle is polled it schedules the task ID in the executor and also sets the task ID in the handle for the wake call above.

If the value hasn't been set yet: the handle is pending. Otherwise return it! That's it!

#### WaitGroups

I use WaitGroups in Go all the time. I think to be more "Rust-y" I would use JoinHandles instead, but I started with WaitGroup because I wasn't sure how tough it would be.

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

## Where did I use AI?

In 2025 (almost 2026) it's a good idea to explicitly say where I used AI.

### Debugging

The answer is that I barely used AI to write code, but I used it liberally to debug code.

One of my favorite bugs I caught with Claude was a performance issue. I was finding that tasks could take nearly 1 millisecond to run. This may not sound like much, but when all a task is doing is executing a bit of parsing code and then enqueueing an entry for IO Uring to submit later in a batch that seemed very very high. When I profiled with `perf` I found hundreds of layers of `Box::pin(Box::pin(...))` which I didn't understand.

[Here is my conversation with Claude](https://claude.ai/chat/3b211270-e5c6-412c-a429-b15ecbeb5d6a) and here is [the commit fix](https://github.com/benhirsch24/twoio/commit/95959c0d0017ee613e26860d731f4a211a8e9ae5#diff-8f324cb85c41284226b8477be31eba1c0a9b7544e2488cb0d4a67ec369b1f586) specifically executor.rs line 95.

Instead of `self.tasks.insert(*task_id, task);` I was doing `self.tasks.insert(*task_id, Box::pin(task))`.

The problem is that `Pin<Box<impl Future>>` implements Future! This means that `Pin<Box<Pin<Box<impl Future>>>>` also implements Future, and down on the line. Resolving this bug reduced the time it took to poll a task from 700us to 50-100us (depending on the task). AI was very helpful for this!

### Writing code

I used AI a little bit to write code. Because I was really doing this to learn instead of implement I really wanted to write most of the code myself. I didn't think that reviewing code written by AI would help me learn quite the same as writing the code myself.

However once I had most of the main pieces in place and fully understood my framework I let it drive a little more. I used Codex mostly because I pay for ChatGPT and don't need two AI subscriptions.

I used it to [rename the project to twoio](https://github.com/benhirsch24/twoio/commit/06b46ca09f797edbbfcaad1f6ad8f539c9a72dc9) which is a perfect task. It's just renaming a bunch of modules and compiling to make sure it compiles properly.

I used it to [implement File](https://github.com/benhirsch24/twoio/commit/2f2ee44056d0f6ea2104d492fd2d088b67f6fc1b). This was also great because I could tell it to follow the pattern I set for TcpStream already. There was some polishing I had to do.

It [refactored stats according to what I told it to do](https://github.com/benhirsch24/twoio/commit/b481060528b1362f89aa9eb64af432884efddc46)

I even had it use the [`channel` implementation I wrote and then it adapted that into my pubsub server](https://github.com/benhirsch24/twoio/commit/a80383810e95880d82dd332e6e6eb96722184cf4)

Overall I think this was a good usage of AI. I implemented the beginning of the project and set out patterns, then I could tactically use AI to write the code I wanted to write but faster.

### Chatting and learning

I did plenty of chatting with ChatGPT and Claude just to learn in general. Sometimes I'll just ask "What is the proper behavior when a Future has been polled to completion? Should it panic if polled again?" or "How does IO Uring SQPOLL mode work?" I use it as a search engine that I can chat with. I also checked out the Linux source code and then used Codex to learn how to trace through the `io_uring` implementation. I found it very helpful for that purpose.

One thing I don't like is that now that I've chatted with ChatGPT about my project so much, it seems to want to bring every conversation back around to it. I'll ask it about Rust Futures and it's like "Here's the answer. This will be very useful for your Rust async runtime project with io uring! Want me to write code for it?" It's like... I know it will be useful but I'm working on it myself. I just wanted an answer about the thing that I asked about.
