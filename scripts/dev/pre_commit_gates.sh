#!/usr/bin/env bash
# scripts/dev/pre_commit_gates.sh
# Executes AST formatting, import sorting, static type audits, transient cache
# purges, and mathematical unit test suites prior to git commit operations.

set -Eeuo pipefail

# -----------------------------------------------------------------------------
# Terminal ANSI Color & Formatting Constants
# -----------------------------------------------------------------------------
readonly YW="\033[33m"
readonly BL="\033[36m"
readonly RD="\033[01;31m"
readonly GN="\033[1;92m"
readonly CL="\033[0m"
readonly BOLD="\033[1m"
readonly TAB="  "

# -----------------------------------------------------------------------------
# Global Execution State
# -----------------------------------------------------------------------------
SKIP_TESTS=0
CLEAN_ONLY=0
AUTO_STAGE=0
SHOW_HEADER=true
TEST_TARGET=""

# -----------------------------------------------------------------------------
# Diagnostic Logging Primitives
# -----------------------------------------------------------------------------
msg_info()    { echo -e "${TAB}${YW}[INFO]${CL} $1..."; }
msg_ok()      { echo -e "${TAB}${GN}[OK]${CL} $1"; }
msg_warn()    { echo -e "${TAB}${YW}[WARN]${CL} $1"; }
msg_error()   { echo -e "${TAB}${RD}[ERROR]${CL} $1" >&2; exit 1; }

# -----------------------------------------------------------------------------
# User Interface & Documentation
# -----------------------------------------------------------------------------
header_info() {
  if [[ "${SHOW_HEADER}" == false ]]; then 
    return 0 
  fi
  cat <<"EOF"
    ____             _ _ _            __     ____  ________ 
   / __ \___  _____(_) (_)__  ____  / /_   / __ \/  _/ __ \
  / /_/ / _ \/ ___/ / / / _ \/ __ \/ __/  / /_/ // // / / /
 / _, _/  __(__  ) / / /  __/ / / / /_   / ____// // /_/ / 
/_/ |_|\___/____/_/_/_/\___/_/ /_/\__/  /_/   /___/_____/  
                                                           
EOF
  echo -e "${BL}${BOLD}Distributed Control System - Development Pre-Commit Pipeline${CL}\n"
}

print_usage() {
  printf "${BOLD}USAGE:${CL}\n"
  printf "  %s [OPTIONS]\n\n" "${0##*/}"
  printf "${BOLD}DESCRIPTION:${CL}\n"
  printf "  Enforces deterministic import sorting, AST code formatting (Ruff),\n"
  printf "  static type verification (Mypy), and mathematical unit test suites (Pytest).\n\n"
  printf "${BOLD}OPTIONS:${CL}\n"
  printf "  -s, --skip-tests       Skip test suite execution (cleanup, formatting, and linting only)\n"
  printf "  -c, --clean-only       Purge cache directories and Python bytecode, then exit\n"
  printf "  -a, --auto-stage       Re-stage modified files in git index after ruff formatting\n"
  printf "  -t, --target <path>    Run tests against a specific test file or directory\n"
  printf "  -q, --no-header        Suppress ASCII banner display\n"
  printf "  -h, --help             Display this help dialog and exit\n"
}

# -----------------------------------------------------------------------------
# Function: parse_cli_arguments
# -----------------------------------------------------------------------------
parse_cli_arguments() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      -h|--help)
        print_usage
        exit 0
        ;;
      -q|--no-header)
        SHOW_HEADER=false
        shift
        ;;
      -c|--clean-only)
        CLEAN_ONLY=1
        shift
        ;;
      -s|--skip-tests)
        SKIP_TESTS=1
        shift
        ;;
      -a|--auto-stage)
        AUTO_STAGE=1
        shift
        ;;
      -t|--target)
        if [[ -n "${2:-}" && ! "$2" =~ ^- ]]; then
          TEST_TARGET="$2"
          shift 2
        else
          msg_error "Option --target requires a non-empty path argument."
        fi
        ;;
      *)
        msg_error "Unknown argument: '$1'. Run '$0 --help' for usage."
        ;;
    esac
  done
}

# -----------------------------------------------------------------------------
# Function: resolve_workspace_root
# -----------------------------------------------------------------------------
resolve_workspace_root() {
  local repo_root
  if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    repo_root="$(git rev-parse --show-toplevel)"
  else
    repo_root="$(pwd)"
    msg_warn "Not inside a valid Git work tree. Defaulting root to: ${repo_root}"
  fi
  cd "${repo_root}"
}

# -----------------------------------------------------------------------------
# Function: verify_toolchain
# -----------------------------------------------------------------------------
verify_toolchain() {
  msg_info "Verifying development toolchain in active environment"
  local required_tools=("ruff" "mypy")
  local missing_tools=()

  for tool in "${required_tools[@]}"; do
    if ! command -v "${tool}" >/dev/null 2>&1; then
      missing_tools+=("${tool}")
    fi
  done

  if [[ ${#missing_tools[@]} -ne 0 ]]; then
    msg_error "Missing required toolchain binary: [${missing_tools[*]}]. Activate virtualenv or run 'pip install -r requirements.txt'."
  fi
  msg_ok "Core toolchain verified (ruff, mypy)"
}

# -----------------------------------------------------------------------------
# Function: purge_transient_caches
# -----------------------------------------------------------------------------
purge_transient_caches() {
  msg_info "Purging Python bytecode and temporary compiler caches"
  
  # Standardize directory purging
  find . -type d \( -name "__pycache__" -o -name ".pytest_cache" -o -name ".mypy_cache" -o -name ".ruff_cache" \) -prune -exec rm -rf {} + 2>/dev/null || true
  
  # Delete leftover bytecode and temporary OS metadata
  find . -type f \( -name "*.pyc" -o -name "*.pyo" -o -name "*~" -o -name ".DS_Store" \) -delete 2>/dev/null || true
  
  msg_ok "Workspace cache purged successfully"
}

# -----------------------------------------------------------------------------
# Function: run_ruff_lint_and_fix
# -----------------------------------------------------------------------------
run_ruff_lint_and_fix() {
  msg_info "Executing deterministic import sorting and linter autofixes (Ruff)"
  if ! ruff check --fix src/ tests/ scripts/; then
    msg_error "Ruff check reported unfixable lint errors. Correct issues identified above."
  fi
  msg_ok "Linter validation and import sorting passed"
}

# -----------------------------------------------------------------------------
# Function: run_ruff_format
# -----------------------------------------------------------------------------
run_ruff_format() {
  msg_info "Enforcing PEP 8 / 88-character formatting standard (Ruff)"
  if ! ruff format src/ tests/ scripts/; then
    msg_error "Ruff code formatting engine encountered an unrecoverable parse error."
  fi
  msg_ok "AST formatting standard enforced"

  # Optional git re-staging for modified tracked files
  if [[ "${AUTO_STAGE}" -eq 1 ]]; then
    if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
      git add -u src/ tests/
      msg_ok "Automatically re-staged formatting modifications in git index"
    fi
  fi
}

# -----------------------------------------------------------------------------
# Function: run_static_type_analysis
# -----------------------------------------------------------------------------
run_static_type_analysis() {
  msg_info "Executing static type verification across package (Mypy)"
  if ! mypy src/; then
    msg_error "Static type verification failed. Resolve type mismatches identified above."
  fi
  msg_ok "Type signatures and generic constraints verified (Mypy)"
}

# -----------------------------------------------------------------------------
# Function: run_test_suites
# -----------------------------------------------------------------------------
run_test_suites() {
  if [[ "${SKIP_TESTS}" -eq 1 ]]; then
    msg_warn "Unit test suite execution bypassed via CLI flag"
    return 0
  fi

  msg_info "Executing automated unit test suites"

  local target_path="tests/"
  if [[ -n "${TEST_TARGET}" ]]; then
    target_path="${TEST_TARGET}"
  fi

  if command -v pytest >/dev/null 2>&1; then
    if ! pytest "${target_path}" -v --tb=short; then
      msg_error "Pytest execution failed."
    fi
  else
    msg_warn "Pytest binary not detected in PATH. Falling back to python3 -m unittest"
    if ! python3 -m unittest discover -s tests -p "test_*.py" -v; then
      msg_error "Unittest runner discovered assertion failures."
    fi
  fi

  msg_ok "All test assertions passed successfully"
}

# -----------------------------------------------------------------------------
# Main Script Orchestrator
# -----------------------------------------------------------------------------
main() {
  parse_cli_arguments "$@"
  resolve_workspace_root
  header_info

  if [[ "${CLEAN_ONLY}" -eq 1 ]]; then
    purge_transient_caches
    printf "\n${GN}${BOLD}✔ Workspace cleanup complete.${CL}\n\n"
    exit 0
  fi

  verify_toolchain
  purge_transient_caches
  run_ruff_lint_and_fix
  run_ruff_format
  run_static_type_analysis
  run_test_suites

  printf "\n${GN}${BOLD}✔ All pre-commit quality gates passed successfully.${CL}\n\n"
}

main "$@"