# Contributing to Discord Music Bot

Thank you for your interest in contributing! This document provides guidelines for contributing to the project.

## 🤝 How to Contribute

### Reporting Bugs

Before reporting a bug, please:
1. Check if the issue already exists in [GitHub Issues](https://github.com/yourusername/discord-music-bot/issues)
2. Test with the latest version
3. Provide detailed information about your environment

When reporting bugs, include:
- **Environment**: OS, Python version, dependencies versions
- **Steps to reproduce** the issue
- **Expected behavior** vs **actual behavior**
- **Error messages** and logs
- **Configuration** (without sensitive data)

### Suggesting Features

For feature requests:
1. Check existing [feature requests](https://github.com/yourusername/discord-music-bot/issues?q=is%3Aissue+is%3Aopen+label%3Aenhancement)
2. Open a new issue with the `enhancement` label
3. Describe the feature and its benefits
4. Consider implementation complexity

### Development Setup

1. **Fork and clone** the repository
```bash
git clone https://github.com/yourusername/discord-music-bot.git
cd discord-music-bot
```

2. **Set up development environment**
```bash
python -m venv venv
source venv/bin/activate  # Linux/macOS
pip install -r requirements.txt
pip install pytest black flake8 bandit
```

3. **Create a feature branch**
```bash
git checkout -b feature/your-feature-name
```

### Code Standards

#### Python Style Guide
- Follow [PEP 8](https://pep8.org/)
- Use [Black](https://black.readthedocs.io/) for formatting
- Maximum line length: 127 characters
- Use type hints where possible

#### Code Quality Tools
```bash
# Format code
black .

# Lint code
flake8 .

# Security check
bandit -r .

# Run all checks
make lint  # if Makefile exists
```

#### Commit Messages
Follow [Conventional Commits](https://www.conventionalcommits.org/):

```
type(scope): description

[optional body]

[optional footer]
```

Types:
- `feat`: New feature
- `fix`: Bug fix
- `docs`: Documentation changes
- `style`: Code style changes
- `refactor`: Code refactoring
- `test`: Test additions/changes
- `chore`: Build process or auxiliary tool changes

Examples:
```
feat(music): add volume control command
fix(voice): resolve connection timeout issues
docs(readme): update installation instructions
```

### Testing

#### Running Tests
```bash
# Test basic functionality
python test_dlp.py
python test_playlist.py
python test_voice.py

# Future: Run unit tests
# pytest tests/
```

#### Adding Tests
- Add tests for new features
- Test edge cases and error conditions
- Mock external dependencies (Discord API, YouTube)

### Pull Request Process

1. **Update documentation** for any new features
2. **Test thoroughly** on multiple environments if possible
3. **Update CHANGELOG.md** if significant changes
4. **Keep PRs focused** - one feature/fix per PR
5. **Write clear descriptions** of changes

#### PR Checklist
- [ ] Code follows style guidelines
- [ ] Self-review completed
- [ ] Tests added/updated
- [ ] Documentation updated
- [ ] No merge conflicts
- [ ] CI/CD passes

### Documentation

#### Types of Documentation
- **Code comments**: Explain complex logic
- **Docstrings**: For all public functions/classes
- **README.md**: Keep up-to-date
- **INSTALLATION.md**: Update for new requirements
- **CLAUDE.md**: Update for architectural changes

#### Documentation Style
- Use clear, concise language
- Provide examples where helpful
- Keep documentation in sync with code

### Security

#### Security Guidelines
- Never commit tokens, passwords, or sensitive data
- Use environment variables for configuration
- Validate user inputs
- Follow Discord's API guidelines
- Report security issues privately

#### Sensitive Data
Files that should never be committed:
- `token` (bot token file)
- `.env` (environment variables)
- `config.json` (if containing secrets)
- Any files with API keys or passwords

### Release Process

1. **Version bump** in relevant files
2. **Update CHANGELOG.md**
3. **Create release PR**
4. **Tag release** after merge
5. **Update Docker images**

### Community Guidelines

#### Code of Conduct
- Be respectful and inclusive
- Provide constructive feedback
- Help newcomers learn
- Focus on the code, not the person

#### Communication
- Use GitHub Issues for bug reports and feature requests
- Use GitHub Discussions for questions and general discussion
- Be patient with response times
- Provide context in communications

### Getting Help

- **Documentation**: Check README.md and INSTALLATION.md first
- **GitHub Issues**: For bugs and feature requests
- **GitHub Discussions**: For questions and general help
- **Code Review**: Ask for feedback in PR comments

## 🏷️ Labels

We use these labels for organization:

- `bug`: Something isn't working
- `enhancement`: New feature or request
- `documentation`: Improvements or additions to docs
- `good first issue`: Good for newcomers
- `help wanted`: Extra attention is needed
- `question`: Further information is requested

## 📋 Development Roadmap

Check our [project roadmap](https://github.com/yourusername/discord-music-bot/projects) for planned features and improvements.

Thank you for contributing! 🎉