#!/usr/bin/env bash
# One-time setup, run on your own computer: let GitHub Actions deploy Orchard
# to the EC2 instance whenever something is merged into main.
#
#   scripts/setup-github-deploy.sh
#   REGION=us-east-1 NAME=orchard-staging scripts/setup-github-deploy.sh
#
# Creates, or updates if they already exist:
#   1. IAM OIDC provider for token.actions.githubusercontent.com, so GitHub can
#      get short-lived AWS credentials. No access keys are stored in GitHub.
#   2. SSM document "Orchard-Update", which can only run
#      /opt/orchard/scripts/update.sh (with or without -y).
#   3. IAM role "orchard-github-deploy". Only workflows on $BRANCH of $REPO can
#      assume it, and it can only send that document to the instance tagged
#      Name=$NAME and read the result. It cannot run other commands.
#   4. GitHub repository variables used by .github/workflows/deploy.yml.
#
# Needs the AWS CLI v2 (signed in as an IAM admin) and the GitHub CLI (gh).
set -euo pipefail

REGION="${REGION:-us-west-2}"
NAME="${NAME:-orchard}"
BRANCH="${BRANCH:-main}"
REPO="${REPO:-$(gh repo view --json nameWithOwner -q .nameWithOwner)}"
ROLE=orchard-github-deploy
DOC=Orchard-Update

ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
OIDC_ARN="arn:aws:iam::$ACCOUNT:oidc-provider/token.actions.githubusercontent.com"
tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT

# The "sub" claim GitHub puts in its OIDC token. Repositories that use immutable
# subjects get "repo:owner@<id>/name@<id>" instead of "repo:owner/name", so ask
# GitHub for the prefix rather than assuming it.
IFS=$'\t' read -r use_default SUB_PREFIX < <(gh api "repos/$REPO/actions/oidc/customization/sub" \
  -q '[(.use_default | tostring), (.sub_claim_prefix // "")] | @tsv')
if [ "$use_default" = false ]; then
  echo "This repository uses a custom OIDC subject template; set the "sub" condition in step 3 of this script by hand." >&2
  exit 1
fi
SUB="${SUB_PREFIX:-repo:$REPO}:ref:refs/heads/$BRANCH"

echo "Repository $REPO ($BRANCH) -> instance Name=$NAME in $REGION"
echo "GitHub token subject: $SUB"

# 1. OIDC provider
if aws iam get-open-id-connect-provider --open-id-connect-provider-arn "$OIDC_ARN" >/dev/null 2>&1; then
  echo "1. OIDC provider: already exists"
else
  aws iam create-open-id-connect-provider --url https://token.actions.githubusercontent.com \
    --client-id-list sts.amazonaws.com >/dev/null
  echo "1. OIDC provider: created"
fi

# 2. SSM document. {{ force }} is limited to "no" or "yes" by allowedValues.
cat > "$tmp/doc.json" <<'EOF'
{
  "schemaVersion": "2.2",
  "description": "Update Orchard to the latest origin/main by running /opt/orchard/scripts/update.sh.",
  "parameters": {
    "force": {
      "type": "String", "default": "no", "allowedValues": ["no", "yes"],
      "description": "yes: restart even if searches, scans or S3 copies are running (they become interrupted)."
    }
  },
  "mainSteps": [{
    "action": "aws:runShellScript", "name": "update",
    "inputs": {
      "timeoutSeconds": "600",
      "runCommand": [
        "export HOME=/root",
        "if [ '{{ force }}' = yes ]; then exec /opt/orchard/scripts/update.sh -y </dev/null; fi",
        "exec /opt/orchard/scripts/update.sh </dev/null"
      ]
    }
  }]
}
EOF
if aws ssm describe-document --region "$REGION" --name "$DOC" >/dev/null 2>&1; then
  if out=$(aws ssm update-document --region "$REGION" --name "$DOC" --content "file://$tmp/doc.json" \
             --document-version '$LATEST' --query DocumentDescription.DocumentVersion --output text 2>&1); then
    aws ssm update-document-default-version --region "$REGION" --name "$DOC" --document-version "$out" >/dev/null
    echo "2. SSM document $DOC: updated to version $out"
  elif grep -q DuplicateDocumentContent <<<"$out"; then
    echo "2. SSM document $DOC: already up to date"
  else
    echo "$out" >&2; exit 1
  fi
else
  aws ssm create-document --region "$REGION" --name "$DOC" --document-type Command \
    --document-format JSON --content "file://$tmp/doc.json" >/dev/null
  echo "2. SSM document $DOC: created"
fi

# 3. Deploy role
cat > "$tmp/trust.json" <<EOF
{"Version": "2012-10-17", "Statement": [{
  "Effect": "Allow",
  "Principal": {"Federated": "$OIDC_ARN"},
  "Action": "sts:AssumeRoleWithWebIdentity",
  "Condition": {"StringEquals": {
    "token.actions.githubusercontent.com:aud": "sts.amazonaws.com",
    "token.actions.githubusercontent.com:sub": "$SUB"
  }}
}]}
EOF
cat > "$tmp/policy.json" <<EOF
{"Version": "2012-10-17", "Statement": [
  {"Sid": "RunOnlyTheUpdateDocument", "Effect": "Allow", "Action": "ssm:SendCommand",
   "Resource": "arn:aws:ssm:$REGION:$ACCOUNT:document/$DOC"},
  {"Sid": "OnlyOnTheOrchardInstance", "Effect": "Allow", "Action": "ssm:SendCommand",
   "Resource": "arn:aws:ec2:$REGION:$ACCOUNT:instance/*",
   "Condition": {"StringEquals": {"ssm:resourceTag/Name": "$NAME"}}},
  {"Sid": "FindInstanceAndReadResult", "Effect": "Allow",
   "Action": ["ssm:DescribeInstanceInformation", "ssm:GetCommandInvocation"], "Resource": "*"}
]}
EOF
if aws iam get-role --role-name "$ROLE" >/dev/null 2>&1; then
  aws iam update-assume-role-policy --role-name "$ROLE" --policy-document "file://$tmp/trust.json"
  echo "3. IAM role $ROLE: trust policy updated"
else
  aws iam create-role --role-name "$ROLE" --assume-role-policy-document "file://$tmp/trust.json" \
    --description "GitHub Actions ($REPO, $BRANCH) runs the $DOC SSM document on the $NAME instance" >/dev/null
  echo "3. IAM role $ROLE: created"
fi
aws iam put-role-policy --role-name "$ROLE" --policy-name orchard-deploy --policy-document "file://$tmp/policy.json"
echo "   permissions: send $DOC to Name=$NAME only, read results"

# 4. Repository variables for the workflow
gh variable set AWS_DEPLOY_ROLE_ARN   --repo "$REPO" --body "arn:aws:iam::$ACCOUNT:role/$ROLE"
gh variable set AWS_REGION            --repo "$REPO" --body "$REGION"
gh variable set ORCHARD_INSTANCE_NAME --repo "$REPO" --body "$NAME"
echo "4. GitHub variables set on $REPO"

echo
echo "Done. Merging into $BRANCH now deploys after the tests pass."
echo "Run a deploy by hand:  gh workflow run deploy.yml --repo $REPO   (add -f force=true to restart during jobs)"
