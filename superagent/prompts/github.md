# Working with GitHub

- Use `gh` for every GitHub operation: reading issues and pull requests, pushing branches, opening pull requests and
  answering review comments. It is authenticated as the agent account.
- Begin every text you post on GitHub yourself (pull request descriptions, review replies, comments made with `gh`)
  with the line: {disclosure}
  Messages you send with `reply` get that line from the host; do not add it there.
- A pull request belongs to this ticket when its branch is named after the ticket. Report its URL in
  `pull_request` so the ticket follows it.
- Read review comments with `gh api repos/{repository}/pulls/<number>/comments` and answer in the review thread.
