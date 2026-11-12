# Working agreements: this repository is public

Everything that leaves this checkout is public and permanent: files, commit
messages, author and committer identities, branch and tag names, pull request
titles and descriptions, review and issue comments, release notes. GitHub's
activity view keeps it after a force-push or a deleted branch, so a leak cannot
be taken back. These rules are for people and AI agents alike.

## Identity

- Commit as `Abijah Kajabika <34766858+AbijahKaj@users.noreply.github.com>`
  (this checkout's `git config user.email` is set to it). Never a personal or
  work address.
- No `Co-Authored-By` or other trailers, tool attributions or session links in
  commits, pull requests or comments.

## Content

Never write any of these into a file, a commit message, a pull request or a comment:

- the names of private repositories, internal services, servers, hosts, IP
  addresses, local paths (`/Users/...`, `/home/...`), dashboards, deploy or
  hosting tooling, or pull request and issue numbers of other repositories;
- anything about a user or customer: names, email addresses, account, design,
  session or job ids, their boards, files or messages;
- secrets: API keys, tokens, passwords, `.env` contents;
- how the hosted Koppar service is built (its models, method, infrastructure).
  Describe what changed for someone using this repository, in plain words.

Keep OS and editor files (`.DS_Store`, swap files) out of commits.

## Before anything is pushed

1. `git log --format='%h %an <%ae> | %cn <%ce>' origin/main..` shows only the
   address above.
2. Read `git log -p origin/main..` in full, and the pull request text, against
   the list above.
3. Nothing is pushed, and no pull request or release is opened, before the
   owner has reviewed it.
