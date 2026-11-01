# Clean-room statement

All code and documentation in this repository were written from scratch for porter.

- Designs are based only on public, documented patterns: the GitHub CLI (`gh`), the
  public GitHub Copilot CLI documentation and README, the `AGENTS.md` convention,
  and the documented headless modes of public coding-agent CLIs.
- No source code, configuration, documents, prompts, file layouts, or internal names
  from any employer, client, or other third party were used, copied, or adapted.
- No third-party code is vendored. The runtime uses only the Python standard
  library. Development tools (`pytest`, `ruff`) are ordinary public dependencies.
- External agent CLIs (for example GitHub Copilot CLI) are invoked as separate
  programs that the user installs and licenses on their own. porter does not
  redistribute them.

Contributors (human or agent) must follow the same rule: write original code, cite
only public sources, and leave out anything you learned under a confidentiality
obligation. If in doubt, do not include it and ask in an issue.
