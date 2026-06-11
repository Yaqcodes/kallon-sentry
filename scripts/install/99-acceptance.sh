#!/usr/bin/env bash
# 99-acceptance.sh — final gate. Delegates to scripts/kallon-acceptance.sh so the
# same checks run standalone or as the last installer module.
source "$(dirname "$0")/lib.sh"

main() {
  local accept="$INSTALL_DIR/../kallon-acceptance.sh"
  [[ -f "$accept" ]] || die "acceptance script not found: $accept"
  # Invoke via bash — repo tracks scripts 644; do not require +x bit.
  exec bash "$accept" --env "$KALLON_ENV"
}

main "$@"
