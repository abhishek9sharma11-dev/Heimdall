# Generative AI Mentor FAQ

## What is generative AI?

Generative AI is a type of computer program that can create new things like text, images, or code just by learning patterns from lots of examples. It works like a very smart autocomplete that has read millions of books and websites.

For example, when you ask ChatGPT a question it does not look up the answer, it generates a new answer based on everything it learned during training.

## What is a prompt?

A prompt is the message or question you send to an AI tool. The better your prompt, the better the AI response you get back. Think of it like giving instructions to a very smart assistant who does exactly what you say.

For example, instead of typing "write something about dogs" you could type "write 3 fun facts about golden retrievers for a 10 year old" and get a much more useful answer.

## What is the difference between ChatGPT and Claude?

Both are AI assistants that answer questions and help with writing, but they are made by different companies. ChatGPT is made by OpenAI and Claude is made by Anthropic. They have different strengths but both can help you with most everyday tasks.

Think of it like two different GPS apps. Both get you to the same place but they have different interfaces and sometimes take slightly different routes.

## What is RAG?

RAG stands for Retrieval Augmented Generation. It means the AI looks up specific information from a set of documents before answering your question so the answer is more accurate and grounded in real facts.

For example, this bot uses RAG to search through the host's newsletters and talks before answering your question so it gives you answers based on what the host actually said.

## What does hallucination mean in AI?

Hallucination is when an AI makes up information that sounds confident but is actually wrong or completely made up. It happens because AI generates text based on patterns, not by checking facts in real time.

For example, if you ask an AI about a very obscure historical event it might invent dates or names that sound plausible but are not real.

## How do I get better answers from AI?

Give the AI more context about who you are and what you need. The more specific you are, the better the answer. Also tell it the format you want, like two sentences only or explain it like I am 12 years old.

For example, instead of asking what is machine learning just ask can you explain machine learning in 2 sentences as if I have never heard of it before.

## What AI tools should a beginner start with?

Start with Claude or ChatGPT for writing and questions, and try Cursor or GitHub Copilot if you want help with code. You do not need to use all tools at once. Pick one and use it deeply for a few weeks before adding more.

Think of it like learning to cook. Master one dish before trying to cook a full menu.

## How do I use AI at work without it replacing my job?

Use AI to handle the repetitive parts of your work so you can spend more time on the creative and strategic parts that only you can do. The people who get replaced are the ones who ignore AI, not the ones who use it.

For example, if you write reports every week use AI to do the first draft and spend your energy on the insights and decisions that need your judgment.

---

## How to use this file

This is the format the bot reads. Drop more files like this into ./knowledge/ with one topic per file such as pricing.md or talks-2025.md and run python scripts/ingest_knowledge.py to rebuild the index.

The bot retrieves the most relevant chunks per question and feeds them to Claude as context. More specific knowledge means better answers.
