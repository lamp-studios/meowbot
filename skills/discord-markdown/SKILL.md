---
name: discord-markdown
description: How to format replies with Discord's markdown (bold, headings, lists, code blocks, quotes, spoilers, timestamps) and what Discord can't render, like tables.
triggers: markdown, format, formatting, formatted, table, tables, code block, codeblock, heading, headings, bold, italic, italics, underline, strikethrough, spoiler, spoilers, bullet points, timestamp
---
# Discord markdown

Discord renders its own flavour of markdown. Use it to make replies easy to read, but only
where it helps: a short casual reply needs no formatting at all.

## Text styles
- `*italic*` or `_italic_`
- `**bold**`
- `***bold italic***`
- `__underline__` (two underscores is underline on Discord, not bold)
- `~~strikethrough~~`
- `||spoiler||` hides text until clicked
- Escape a symbol with a backslash, like `\*not italic\*`

## Headings and subtext
Headings only work at the very start of a line, with a space after the hashes:
- `# Big heading`
- `## Medium heading`
- `### Small heading`
- `-# small grey subtext` (good for footnotes and side remarks)
There is no `####` or smaller. Don't put a heading on a one line answer.

## Lists
- Start a line with `- ` or `* ` for bullets, or `1. ` for numbered lists.
- Indent with two spaces to nest a bullet under another one.

## Quotes
- `> text` quotes a single line.
- `>>> text` quotes everything after it, to the end of the message.

## Code
- Inline code: wrap it in single backticks, like `` `pip install requests` ``.
- Code blocks: three backticks on their own line, with the language right after the
  opening ones so it gets syntax highlighting:

```py
print("hello")
```

Common language tags: `py`, `js`, `ts`, `json`, `bash`, `c`, `cpp`, `cs`, `java`, `rust`,
`go`, `html`, `css`, `sql`, `yaml`, `diff` (lines starting with `+` turn green and `-` red).

## Links
- `[text](https://example.com)` makes a masked link.
- Wrapping a link in angle brackets, `<https://example.com>`, stops Discord showing a big
  preview embed. Do this when you list several links.

## Discord specific syntax
- Timestamps: `<t:UNIX_SECONDS:STYLE>` shows a time in every reader's own timezone.
  Styles: `t` short time, `T` long time, `d` short date, `D` long date, `f` date and time,
  `F` full date and time, `R` relative ("in 2 hours"). Example: `<t:1767225600:R>`.
- Mentions: `<@USER_ID>` for a user, `<#CHANNEL_ID>` for a channel, `<@&ROLE_ID>` for a role.

## What Discord does NOT render
- **Tables.** Pipes and dashes show up as raw text. Use a bullet list instead, or line the
  columns up inside a code block:
```
Name     Score
Alice    12
Bob      9
```
- Horizontal rules (`---`, `***`) are not rendered. Use a blank line or a heading instead.
- Images from markdown (`![alt](url)`) don't work. Paste the bare link and Discord embeds it.
- HTML tags, footnotes and task lists (`- [ ]`) show up as plain text.

## Limits
A single message is at most 2000 characters. Keep formatted answers compact: a few short
sections beat one wall of headings.
