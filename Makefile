up:
	docker compose up --build -d

down:
	docker compose down

logs:
	docker compose logs -f --tail=100

test:
	docker build --target test -t lyftr-backend-test ./backend
	docker run --rm lyftr-backend-test

.PHONY: up down logs test
