```markdown
# agent-control-plane Development Patterns

> Auto-generated skill from repository analysis

## Overview
This skill teaches the development patterns and conventions used in the `agent-control-plane` Python repository. You'll learn about the project's coding style, commit message conventions, file organization, and how to write and run tests. The guide also provides suggested commands for common workflows to streamline your development process.

## Coding Conventions

### File Naming
- **Pattern:** camelCase
- **Example:**  
  ```python
  agentManager.py
  configLoader.py
  ```

### Import Style
- **Pattern:** Relative imports
- **Example:**  
  ```python
  from .utils import parseConfig
  from .models.agent import Agent
  ```

### Export Style
- **Pattern:** Named exports (explicitly listing what is exported)
- **Example:**  
  ```python
  __all__ = ['AgentManager', 'ConfigLoader']
  ```

### Commit Messages
- **Pattern:** Conventional commits
- **Prefix:** `feat`
- **Average Length:** 54 characters
- **Example:**  
  ```
  feat: add agent registration endpoint
  ```

## Workflows

### Feature Development
**Trigger:** When implementing a new feature  
**Command:** `/feature-dev`

1. Create a new branch for your feature.
2. Write code using camelCase file naming and relative imports.
3. Use named exports for module interfaces.
4. Commit changes using the `feat:` prefix and a concise message.
5. Open a pull request for review.

### Testing
**Trigger:** When writing or running tests  
**Command:** `/run-tests`

1. Create test files following the `*.test.*` pattern (e.g., `agentManager.test.py`).
2. Write test cases for new or modified code.
3. Run tests using your preferred Python test runner.
4. Ensure all tests pass before merging.

## Testing Patterns

- **Framework:** Not specified (choose your preferred Python test framework, e.g., `unittest` or `pytest`)
- **File Pattern:** Test files are named using the `*.test.*` pattern.
- **Example:**  
  ```python
  # agentManager.test.py
  import unittest
  from .agentManager import AgentManager

  class TestAgentManager(unittest.TestCase):
      def test_register_agent(self):
          manager = AgentManager()
          self.assertTrue(manager.register('agent1'))
  ```

## Commands
| Command         | Purpose                                      |
|-----------------|----------------------------------------------|
| /feature-dev    | Start a new feature development workflow      |
| /run-tests      | Run all test files matching `*.test.*`        |
```
