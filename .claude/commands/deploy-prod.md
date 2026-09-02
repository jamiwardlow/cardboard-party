Deploy the app to production App Engine.

1. Confirm with the user that they want to deploy to **production** (`cardboard-party`) before proceeding.
2. Run `gcloud app deploy --project=cardboard-party` and stream the output.
3. Once the deploy succeeds, run `gcloud app browse --project=cardboard-party` to get the live URL and confirm the app is up.
