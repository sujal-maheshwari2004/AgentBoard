.PHONY: install-skill test

SKILL_SRC := $(abspath skill/whiteboard)
SKILL_DST := $(HOME)/.claude/skills/whiteboard

install-skill:
	@mkdir -p $(HOME)/.claude/skills
	@if [ -e "$(SKILL_DST)" ] || [ -L "$(SKILL_DST)" ]; then \
		echo "skill already installed at $(SKILL_DST)"; \
	else \
		ln -s "$(SKILL_SRC)" "$(SKILL_DST)" && echo "linked $(SKILL_DST) -> $(SKILL_SRC)"; \
	fi

test:
	uv run pytest
