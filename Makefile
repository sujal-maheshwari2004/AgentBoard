.PHONY: install-skill install-agents test

SKILL_SRC := $(abspath skill/whiteboard)
SKILL_DST := $(HOME)/.claude/skills/whiteboard
AGENTS_DST := $(HOME)/.claude/agents

# Agent definitions are COPIES with a provenance stamp, never symlinks: a dangling symlink makes
# `subagent_type: whiteboard-task` fail silently at dispatch time. A hand-edited file is reported
# as user-modified and left alone.
install-agents:
	@uv run python -m whiteboard.agents_install --dest "$(AGENTS_DST)"

install-skill: install-agents
	@mkdir -p $(HOME)/.claude/skills
	@if [ -e "$(SKILL_DST)" ] || [ -L "$(SKILL_DST)" ]; then \
		echo "skill already installed at $(SKILL_DST)"; \
	else \
		ln -s "$(SKILL_SRC)" "$(SKILL_DST)" && echo "linked $(SKILL_DST) -> $(SKILL_SRC)"; \
	fi

test:
	uv run pytest
