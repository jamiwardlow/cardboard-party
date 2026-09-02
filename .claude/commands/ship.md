Commit staged/unstaged changes, push to GitHub, and deploy to staging.

1. Run `git status` and `git diff HEAD` to review what will be committed. If there is nothing to commit, say so and stop.
2. Draft a concise commit message focused on *why* the change was made (not just what). Show it to the user and ask them to confirm or adjust it before committing.
3. Stage all modified/untracked files that belong to the project (skip `.env`, secrets, binaries). Prefer `git add <specific files>` over `git add -A`.
4. Commit with the confirmed message, co-authored by Claude:
   ```
   git commit -m "$(cat <<'EOF'
   <message>

   Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
   EOF
   )"
   ```
5. Push to GitHub: `git push origin main`.
6. Deploy to staging: `gcloud app deploy staging.yaml --project=cardboard-party-staging` — stream the output.
7. Report success and remind the user to verify the change on staging before deploying to prod.
