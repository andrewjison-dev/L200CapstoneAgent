.PHONY: help install run-ui adk-web adk-run adk-eval test eval lint terraform-fmt

help:
	@echo "Andrew's Family Meal Command Center (L200 Capstone Agent)"
	@echo "  make install        Install ADK and runtime dependencies"
	@echo "  make run-ui         Launch the simplified Family Meal Command Center UI"
	@echo "  make adk-web        Launch Google ADK interactive developer UI (adk web)"
	@echo "  make adk-run        Run the ADK Coordinator CLI session (adk run)"
	@echo "  make adk-eval       Run ADK CLI evaluation suite against concierge_eval.evalset.json"
	@echo "  make test           Run unit and security test suite"
	@echo "  make eval           Run golden dataset regression evaluation harness"

install:
	pip install -r requirements.txt

run-ui:
	python3 -m concierge_agent.ui.server --port 8080

adk-web:
	adk web .

adk-run:
	adk run concierge_agent

adk-eval:
	adk eval concierge_agent evals/concierge_eval.evalset.json

test:
	python3 -m unittest discover -s tests -v

eval:
	python3 -m evals.run_evaluation
