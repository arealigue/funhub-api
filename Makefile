run:
	uvicorn app.main:create_app --reload

test:
	pytest
