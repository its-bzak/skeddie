# AGENTS.md

## 1. General Principles

- Prioritize clarity, maintainability, consistency, and simplicity.
- Follow existing patterns and architecture before introducing new ones.
- Avoid overengineering. Implement the simplest solution that fully addresses the task.
- Make changes only within the scope of the requested work.
- Do not introduce unnecessary dependencies, abstractions, or configuration.
- Write code that is readable and understandable without excessive comments.
- Favor explicit, predictable behavior over clever or implicit behavior.
- Do not assume functionality exists; inspect the codebase before making changes.

## 2. Workflow & Expectations

Before making changes:

- Review the relevant files and understand the existing implementation.
- Check the project's documentation and configuration files.
- Identify established conventions and reuse them.
- Understand the intended behavior before modifying code.

When implementing changes:

- Break complex tasks into smaller, logical steps.
- Keep changes focused and avoid unrelated modifications.
- Preserve existing functionality unless the task explicitly requires changing it.
- Update documentation when functionality, configuration, or workflows change.
- Avoid leaving unfinished code, temporary workarounds, or debugging artifacts.

After making changes:

- Review the diff for unintended modifications.
- Run relevant tests, linters, formatters, and validation commands.
- Address any failures caused by your changes.
- Summarize what changed and mention any checks that were not run.

## 3. Formatting & Code Style

- Use the formatting conventions already established in the repository.
- Respect configuration files for formatting and linting.
- Use consistent indentation throughout each file.
- Avoid unnecessarily long functions, lines, or complex expressions.
- Separate logical sections with whitespace for readability.
- Remove trailing whitespace.
- Ensure files end with a newline.
- Prefer readable code over compressed or overly terse syntax.
- Use comments to explain *why* something is done, not merely *what* the code does.
- Avoid redundant comments, outdated documentation, and commented-out code.

### Formatting precedence

Follow formatting rules in this order:

1. Existing project configuration (formatter, linter, editor config).
2. Established patterns within the relevant directory.
3. Language-specific standards.
4. These guidelines.

Do not manually override automatic formatting conventions without a good reason.

## 4. Naming Conventions

Use descriptive, consistent, and context-appropriate names.

- Names should communicate purpose, not implementation details.
- Avoid vague names such as `data`, `thing`, `stuff`, `temp`, or `foo` unless the scope is trivial.
- Avoid unnecessary abbreviations.
- Use consistent terminology throughout the project.
- Boolean variables should read naturally as conditions (e.g., `isEnabled`, `hasAccess`, `canSubmit`).
- Functions and methods should generally describe an action.
- Classes, types, and components should describe the entity they represent.

### General naming defaults

| Element                  | Convention                                        |
| ------------------------ | ------------------------------------------------- |
| Variables & functions    | `camelCase`                                       |
| Classes & types          | `PascalCase`                                      |
| Components               | `PascalCase`                                      |
| Constants                | `UPPER_SNAKE_CASE` when appropriate               |
| Files & folders          | Follow existing project convention                |
| Private/internal members | Follow language-specific convention               |
| Test files               | Clearly identify the unit or feature being tested |

These are defaults, not mandates. Follow the language and framework's idiomatic conventions where they differ.

## 5. Architecture & Code Organization

- Keep responsibilities separated.
- Avoid mixing unrelated business logic, presentation, data access, and infrastructure concerns.
- Prefer small, cohesive modules over large, multipurpose files.
- Keep functions focused on a single responsibility where practical.
- Avoid circular dependencies.
- Do not introduce abstractions until they provide clear value.
- Reuse existing utilities and shared logic when appropriate.
- Avoid duplicated logic when a clean shared solution makes sense.
- Do not create generic utilities for one-off operations without a clear benefit.
- Keep public interfaces stable unless a change is required.

## 6. Error Handling & Logging

- Handle errors at the appropriate layer.
- Never silently swallow errors.
- Avoid empty catch blocks.
- Provide useful, actionable error messages.
- Do not expose secrets, credentials, tokens, or sensitive user data in logs.
- Use the project's existing logging mechanisms.
- Avoid excessive or noisy logging.
- Validate external input at appropriate boundaries.
- Fail safely and predictably.

## 7. Testing Requirements

- Add or update tests for behavior that changes.
- Prefer meaningful tests that validate observable behavior.
- Include edge cases and failure conditions where appropriate.
- Avoid brittle tests tied to implementation details.
- Do not modify tests simply to make them pass unless the expected behavior has legitimately changed.
- Do not remove test coverage without justification.
- Ensure tests are deterministic and independent whenever practical.

Before completing work:

- Run the most relevant targeted tests.
- Run the broader test suite when practical.
- Run linting and formatting checks when available.
- Run build, type-check, or validation commands when relevant.

If tests cannot be run, explain why.

## 8. Git & Commit Conventions

### Commit messages

Use Conventional Commits:

`<type>(<optional scope>): <description>`

Allowed types:

| Type       | Purpose                                    |
| ---------- | ------------------------------------------ |
| `feat`     | Introduce a new feature                    |
| `fix`      | Fix a bug                                  |
| `refactor` | Restructure code without changing behavior |
| `perf`     | Improve performance                        |
| `docs`     | Documentation-only changes                 |
| `style`    | Formatting or stylistic changes            |
| `test`     | Add or update tests                        |
| `build`    | Build system or dependency changes         |
| `ci`       | CI/CD configuration changes                |
| `chore`    | Maintenance tasks                          |
| `revert`   | Revert a previous commit                   |

Examples:

- `feat(auth): add password reset flow`
- `fix(api): handle missing user identifiers`
- `refactor(core): simplify validation pipeline`
- `docs: update local development instructions`
- `test(parser): cover malformed input`
- `chore: update dependencies`

### Commit practices

- Keep commits focused on a single logical change.
- Write commit messages in imperative mood.
- Keep the subject concise and descriptive.
- Avoid vague messages such as `updates`, `fixes`, `misc`, or `stuff`.
- Do not combine unrelated changes in one commit.
- Never commit generated files, secrets, local configuration, or temporary artifacts unless explicitly required.
- Do not rewrite shared Git history unless explicitly requested.
- Do not commit on behalf of the user unless requested.

## 9. Dependencies & Package Management

- Prefer existing dependencies over introducing new ones.
- Add a dependency only when it provides clear value.
- Check compatibility, maintenance status, licensing, and security implications.
- Avoid redundant packages that overlap with existing functionality.
- Use the project's established package manager.
- Do not mix package managers.
- Keep lockfiles synchronized with dependency changes.
- Avoid unnecessary dependency upgrades unrelated to the task.
- Never install packages globally to work around project configuration without justification.

## 10. Security & Privacy

- Never hardcode secrets, tokens, passwords, private keys, or credentials.
- Use environment variables or approved secret-management systems.
- Never commit `.env` files containing sensitive information.
- Validate and sanitize untrusted inputs appropriately.
- Follow least-privilege principles.
- Avoid unsafe execution, injection vulnerabilities, and insecure defaults.
- Protect personally identifiable and sensitive information.
- Do not disable security checks to make a build, test, or workflow pass.
- Review security implications when changing authentication, authorization, networking, storage, or input handling.

## 11. Documentation

- Keep documentation accurate and concise.
- Document non-obvious architectural decisions.
- Update setup instructions when development workflows change.
- Document new environment variables and configuration options.
- Include examples where they clarify usage.
- Do not document behavior that is not implemented.
- Prefer updating existing documentation over creating redundant documents.

## 12. Configuration & Environment

- Avoid modifying configuration files unless required.
- Preserve compatibility with the supported environment.
- Do not change environment-specific settings without understanding their impact.
- Keep machine-specific configuration out of version control.
- Use existing configuration patterns for feature flags, environment variables, and tooling.
- Ensure new configuration options have sensible defaults where appropriate.

## 13. AI Agent Rules

AI coding agents working in this repository must:

- Read this file before beginning work.
- Inspect relevant code before proposing or implementing changes.
- Treat existing project conventions as authoritative.
- Never fabricate test results, execution results, or implementation details.
- Never claim a task is complete without verifying the changes to the extent possible.
- Ask for clarification when requirements are ambiguous and guessing could lead to significant consequences.
- Make reasonable assumptions for low-risk ambiguity and document them when useful.
- Avoid overwriting user changes.
- Do not revert or discard unrelated modifications.
- Never execute destructive commands without explicit authorization.
- Never commit, push, publish, deploy, or release unless explicitly requested.
- Provide a concise summary of changes and validation performed when finishing a task.

## 14. Completion Criteria

A task is considered complete when:

- [ ] The requested behavior has been implemented.
- [ ] Changes are scoped and relevant.
- [ ] Code follows project conventions.
- [ ] Appropriate tests have been added or updated.
- [ ] Relevant tests and validation checks have been run.
- [ ] No unrelated changes have been introduced.
- [ ] Documentation has been updated where necessary.
- [ ] Remaining limitations or unverified items are clearly disclosed.

## 15. Guiding Rule

When in doubt, choose the solution that is easiest for another developer to understand, maintain, verify, and safely extend.
