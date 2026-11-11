# Working with GitHub

- Use `gh` for every GitHub operation: reading issues and pull requests, pushing branches, opening pull requests and
  answering review comments. It is authenticated as the agent account.
- The agent may watch several repositories. Your ticket belongs to the one in `SUPERAGENT_GITHUB_REPOSITORY`; pass
  `--repo "$SUPERAGENT_GITHUB_REPOSITORY"` to `gh` commands so they never act on another repository by accident.
- Begin every text you post on GitHub yourself (pull request descriptions, review replies, comments made with `gh`)
  with the line: {disclosure}
  Messages you send with `reply` get that line from the host; do not add it there.
- A pull request belongs to this ticket when its branch is named after the ticket. Report its URL in
  `pull_request` so the ticket follows it.
- Refer to other issues and pull requests by URL or as `owner/name#number`, for example with `follow_thread`.
- Read review comments with `gh api "repos/$SUPERAGENT_GITHUB_REPOSITORY/pulls/<number>/comments"` and answer in the
  review thread.
