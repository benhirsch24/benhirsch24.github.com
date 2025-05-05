---
Date: 2025-05-04
---

# Recent thoughts on AI

Wanted to jot down some of my recent thoughts on "AI" and specifically the developer tools I've played around with lately.

## Code, not English

One thing I refuse to use AI for is writing English.

I've seen tools that purport to write tech specs for you, but there's something about taking a list of bullet points and using an AI to expand it into a tech doc format that misses the purpose of writing the doc. On the other end the reader may take a doc and use an AI to condense it into a list of bullet points. Sure, you could argue that this is a signal that the doc itself is a process without meaning and we should cut out the middleman and exchange terse slack messages.

But to me writing is one of the best ways to think. Maybe I myself am a big LLM, but by writing words down on a page (or in a Google doc or in a terminal) it forces me to think through the subject I'm writing about. Then by editing I'm able to consider my audience and think about how to frame my argument for them. There's a bit of a gamesmanship aspect to this - who is going to read this and how do I set up my argument for them? Then I tend to think about what questions they might ask and how I would answer them. It all leads to me understanding the domain much better.

Feeding bullet points to Sonnet and spitting the result out for a tech spec loses all of the benefit for the user.

Now, what I do like:

## CLI-native Tools

I really like tools like Claude Code and Amazon Q CLI. Since I work at the Zon I use Q pretty often and I find it nice. As a vim user it "feels" good to have a terminal-native app. It's easy to open two tabs for development, one for vim and one for Q and iterate.

I'm pretty impressed at the iteration speed of the team behind Q, it seems like there is both internal excitement which is quickly making its way to the external product. Engineers always produce their best work when they're excited IMO, and for some reason we love automating our work.

I've been using Claude Code at home (ie personal projects) and it's a similarly nice experience.

What I enjoy about these tools is I can "enqueue" a request, let the AI go work on it while I do something else (usually answer Slack, work on a doc, or have AI write SQL for a report), and then come back and see what it's done. It lowers the friction to get in the flow because I don't have to load the context into my brain.

Now if AI goes off the rails it does present some friction for me to load the context myself and start rooting around in what it's done, but there's already been some prior exploration so I can in the flow a bit faster.

It's also great for language processing. If I have a blob of text I want to extract certain fields/patterns out of I can just put it in a file and tell AI to grab the parts I'm interested in by giving it an example.

## Plan, then Do

We recently got access to Cline at work which seems to be very popular outside of the Amazon bubble. I've played with it for one or two changes and it seems nice. I'm not a VSCode guy so that presents friction for me, but I like the plan/act cycle.

That seems to be a pattern internally that people have adopted which works very well. First you go jot down your basic idea; then you go back and forth with the AI (Q/Claude/Cline Plan mode) for a bit to get a plan; then you start doing.


I like having Q or Claude Code write the plan down into a directory. I'll say "Great, write this plan into planning/this-feature-plan.md in a way that a dev can execute". Then I'll say "Read this plan and do the first item". If you can distill the essence of the codebase down into a file or two as well that helps as well. Keeping the AI focused by continually reiterating "Read the plan and do number 2" seems to be very successful.

Cline has its Plan mode and I don't have a good intuition yet for how focused the AI can stay with all of the context in Plan. I'll probably continue to use it for the next week or two and see.

## Website (ChatGPT and the rest).

Great for writing SQL. I pay for ChatGPT Plus and use it for some personal projects. Amazon has a few internal website experiences that I use all the time for writing SQL. It's very refreshing to say "I have this table with columns a, b and I want to join it with another table with columns b, c and I want to see this histogram from that. SQL thanks."

I don't have much else to really say about this other than my wife and I brainstormed ideas for a tartar sauce themed restaurant while we were in Hawaii and it was fun.

![](images/tartar-me-up.webp)

I'm just saying, fish and chips shops need to have unlimited tartar sauce available to me. Sure, there's obvious dangers to this.

![](images/tartar-me-up-more.webp)

## Vibe Coding?

With Q/Claude I find I'm way more likely to go into vibe coding mode. Something about the CLI interface lends itself to that. With Cline I felt the integation made it a lot easier for me to review each change it wanted to make which IS positive, but also made the change take much much longer. There's probably a middle ground there depending on the change.

What I really enjoy though is using Q as a scripting language. I'll write a text file that's just

```
Execute this:

1. Query this API for some data
2. Extract values according to this criteria
3. For each result do another thing
4. Summarize output
```

And then open the app (Q/Claude) and say "read `instructions.txt` and do it" and most of the time it gets it right.

In one sense that's nice, but for that purpose is AI just replacing scripting? And if so, isn't that wasteful?

Yes and no. I think it's great for exploratory scripting. My original idea might be wrong, and if so I can iterate within the repl session. Once we've got it right and it's an action I'll want to perform over and over I can tell the AI "Great, write these steps down as a python script that I can execute later".

## Blog

Anyways, new blog post for the first time in a while. Going to try to write more, but judging by the last time I decided to write more we'll see.

Claude created this blog. I'm loving the idea I've seen around of a minimal blog that I just ask AI to fill in the blanks rather than some bespoke blogging static site generator. It made everything based on a couple instructions. It made some mistakes but I fixed them pretty quick. I spent ~$5 doing it with Claude Code.
