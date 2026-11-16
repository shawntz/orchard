#!/usr/bin/env bash
# Open a shell on the Orchard EC2 instance through SSM Session Manager.
#
#   ./login                  # region us-west-2, instance tagged Name=orchard
#   REGION=us-east-1 ./login
#   NAME=orchard-staging ./login
#   AWS_PROFILE=lab ./login
#
# Needs the AWS CLI v2 and the Session Manager plugin. Finds the instance by its
# Name tag each time, so it keeps working if the instance is replaced.
set -euo pipefail

REGION="${REGION:-${AWS_REGION:-us-west-2}}"
NAME="${NAME:-orchard}"

die() { echo "login: $*" >&2; exit 1; }

command -v aws >/dev/null || die "AWS CLI not found. Install AWS CLI v2 first."
command -v session-manager-plugin >/dev/null ||
  die "Session Manager plugin not found. See https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager-working-with-install-plugin.html"

# Refresh AWS credentials if they have expired.
if ! aws sts get-caller-identity --region "$REGION" >/dev/null 2>&1; then
  echo "AWS credentials missing or expired. Running 'aws login'..." >&2
  aws login || die "AWS sign-in failed."
fi

INSTANCE=$(aws ec2 describe-instances --region "$REGION" \
  --filters "Name=tag:Name,Values=$NAME" "Name=instance-state-name,Values=running" \
  --query 'Reservations[].Instances[].InstanceId' --output text)

case "$INSTANCE" in
  "" | None)
    STATE=$(aws ec2 describe-instances --region "$REGION" \
      --filters "Name=tag:Name,Values=$NAME" \
      --query 'Reservations[].Instances[].[InstanceId,State.Name]' --output text)
    [ -n "$STATE" ] && die "no running instance named '$NAME' in $REGION. Found: $STATE"
    die "no instance named '$NAME' in $REGION."
    ;;
  *[[:space:]]*)
    die "more than one running instance named '$NAME' in $REGION: $INSTANCE"
    ;;
esac

echo "Connecting to $INSTANCE ($NAME, $REGION)..." >&2
exec aws ssm start-session --region "$REGION" --target "$INSTANCE"
