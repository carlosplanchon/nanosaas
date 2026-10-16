# NanoSaaS

NanoSaaS is a lightweight microservice platform for managing and tracking computational tasks. Designed with simplicity and scalability in mind, NanoSaaS is perfect for developers and small teams looking to automate background processes efficiently.

I built this system in just three days with almost no prior knowledge of web design or frontend development. While it works, the code quality might not adhere to the best practices, especially in terms of maintainability and structure. I encourage you to check the GitHub issues section, where I've documented the limitations and areas for improvement that I'm currently aware of.

---

## Features

- **Task Management**: Run and monitor background tasks using TaskIQ.
- **Real-Time Updates**: Track task statuses and results in real time.
- **Secure Authentication**: Google SSO for easy and secure login.
- **Credits System**: Users are assigned credits to manage task execution, ensuring controlled resource usage.
- **Data Persistence**: PostgreSQL with Tortoise ORM for secure data management.
- **Modern UI**: Responsive frontend with Alpine.js and Tailwind CSS.
- **Scalable Architecture**: Built with FastAPI and Docker-ready.

### DeepWiki Docs: [https://deepwiki.com/carlosplanchon/nanosaas](https://deepwiki.com/carlosplanchon/nanosaas)

---

## Tech Stack

- **Backend**: FastAPI, TaskIQ, Redis
- **Frontend**: Alpine.js, Tailwind CSS, Prism.js (to color the results' JSON)
- **Database**: PostgreSQL, Tortoise ORM (and its migrations)
- **Authentication**: Google SSO

---

## Installation

1. Clone the repository:
    ```bash
    git clone https://github.com/carlosplanchon/nanosaas.git
    cd nanosaas
    ```

2. Create the `config.toml` file in the root directory, from the provided template:
    ```bash
    cp config_template.toml config.toml
    ```
    Then fill in the `[api]` section:
    ```toml
    [api]
    # --- Internal Keys --- #
    JWT_SIGNING_SECRET_KEY = "your_secret_key"  # Used to sign JWTs; ensure this is a strong, secret key, at least 32 characters long.

    # --- Third parties keys --- #
    GOOGLE_CLIENT_ID = "your_google_client_id"
    GOOGLE_CLIENT_SECRET = "your_google_client_secret"
    GOOGLE_REDIRECT_URI = "http://127.0.0.1:8000/auth/callback"  # Must be an authorized redirect URI of your Google OAuth client.
    ```
    You can generate `JWT_SIGNING_SECRET_KEY` with `python3 scripts/generate_jwt_secret_key.py`. The rest of the template already works with Docker: `[database]` and `[taskiq]` point to the PostgreSQL and Redis of `compose.yaml`, and `ROOT_PATH` is empty, to serve the app at the root.

    `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET` come from a Google OAuth client. Create one in the Google Cloud console, in the [Google Auth Platform](https://console.developers.google.com/auth/clients):
    1. Select or create a project. If the console asks you to, click **Get started** to register the app: its name, a user support email, **External** as its audience, so any Google account can log in, and a contact email.
    2. In **Clients**, click **Create client**, choose **Web application**, and add `http://127.0.0.1:8000/auth/callback` to **Authorized redirect URIs**. It must match `GOOGLE_REDIRECT_URI`.
    3. Copy the client ID and the client secret into `config.toml`. Google shows the whole secret only once, when it creates the client.

    The app only asks Google for the user's name, email and picture, so any Google account can log in while the Google app is in testing: you don't need to add test users or publish it.

3. Start the app with Docker Compose:
    ```bash
    docker compose up --build
    ```
    It runs PostgreSQL, Redis, the backend and a TaskIQ worker, and serves the app at `http://127.0.0.1:8000`, only to this machine. Add `-d` to run it in the background.
    - After changing the code, run the same command again. After changing `config.toml`, run `docker compose restart api worker`.
    - The backend creates the tables the first time, and applies new migrations whenever it starts (see the Development Notes).
    - The data is kept in Docker volumes, so it survives restarts. `docker compose down -v` deletes it.

### Without Docker

Requires Python 3.11 or newer, PostgreSQL and Redis 6 or newer.

1. Set up the environment:
    ```bash
    python3 -m venv .venv
    source .venv/bin/activate
    pip install -r requirements.txt
    ```

2. In `config.toml`, point `[database]` and `[taskiq]` to your PostgreSQL and Redis:
    ```toml
    [database]
    DB_HOST = "your_db_host"
    DB_PORT = 5432  # Ensure this is an integer.
    DB_NAME = "your_database_name"
    DB_USERNAME = "your_db_username"
    DB_PASSWORD = "your_db_password"

    [taskiq]
    # Local Redis:
    broker="redis://127.0.0.1:6379/0"
    backend="redis://127.0.0.1:6379/0"
    ```
    The backend creates the tables, and applies new migrations, when it starts. To run Redis in Docker: `docker run -p 127.0.0.1:6379:6379 redis`. Binding the port to `127.0.0.1` keeps Redis reachable only from this machine: ports published by Docker skip firewalls like ufw.

3. Start the backend:
    ```bash
    uvicorn main_api:app --reload
    ```

4. Start the TaskIQ worker in another terminal, with the virtual environment activated:
    ```bash
    taskiq worker taskiq_task:broker --receiver taskiq_task:Receiver
    ```
    `--receiver` makes it drop the tasks that waited in the queue longer than `queue_timeout`.

Run both commands from the repository root: `config.toml`, the templates and the static files are loaded from the current directory.

### On a server, with HTTPS

To serve the app on your domain, run it with [Caddy](https://caddyserver.com/), which gets the HTTPS certificate and renews it by itself. It's in `compose.yaml`, under the `https` profile.

1. Point your domain to the server, and open the server's ports 80 and 443: Caddy needs both to get the certificate.
2. In the Google Auth Platform, add your domain to **Authorized domains**, in **Branding**. Then add `https://your-domain.com/auth/callback` to the **Authorized redirect URIs** of your client, and set `GOOGLE_REDIRECT_URI` to it in `config.toml`.
3. Create a `.env` file next to `compose.yaml`, with your domain and the `https` profile:
    ```bash
    DOMAIN=your-domain.com
    COMPOSE_PROFILES=https
    ```
    Then start the app with `docker compose up --build -d`, as before: the profile makes every `docker compose` command include Caddy.

Without `DOMAIN`, Caddy serves `https://localhost`, with a certificate of its own that browsers warn about: that way you can try HTTPS on your machine.

---

## Usage

1. Access the application at `http://127.0.0.1:8000`.
2. Log in using Google SSO.

   ![Login Page](docs/login.png)

3. Create and manage tasks via the user dashboard: choose a task, fill in its parameters and start it.

   ![User Panel](docs/user_panel.png)

   It shows the latest 50 tasks (`TASK_HISTORY_LIMIT` in `main_api.py`). It checks them every 5 seconds while one is pending, every 30 seconds otherwise, and not while its tab is hidden.

4. Use the API with the key from the *API Key Management* page, sent in the `X-API-Key` header. A task starts with a POST to `/api/task/<name>`, with its parameters in a JSON object:
    ```bash
    curl -H "X-API-Key: your_api_key" http://127.0.0.1:8000/api/user_credits
    curl -H "X-API-Key: your_api_key" -H "Content-Type: application/json" -d '{"x": 10, "y": 4}' http://127.0.0.1:8000/api/task/divide
    curl -H "X-API-Key: your_api_key" http://127.0.0.1:8000/api/task_details/your_task_id
    ```
   Wrong parameters get a 422 error, without spending a credit. `GET /api/task/divide/10/4` still works for older clients, and so does the `api_key` query parameter, but avoid the latter: URLs end up in server and proxy logs. The interactive docs at `/docs` list every route.

   The page shows the whole key only when you generate it: the database keeps just its first characters and a hash of it.
5. Each user starts with a predefined number of credits (25 by default, set in `web/db_models.py`). A credit is reserved when a task is queued and given back if the task fails, so only successful tasks are charged. When credits run out, the user can't start more tasks until you add more, giving the email the user logs in with:
    ```bash
    docker compose exec api python scripts/add_credits.py user@example.com 25
    ```
   Without Docker, run `python scripts/add_credits.py user@example.com 25` from the repository root. To look into the database itself, `docker compose exec postgres psql -U nanosaas` opens a shell in it.

   A task that waits in the queue longer than `queue_timeout` seconds is dropped, failing as `TaskExpired`, and one that runs longer than `time_limit` seconds is stopped, failing as `TimeoutError` (10 minutes each by default, set in `config.toml`): either way its credit comes back. A task that never ends, for example because its worker died, fails the same way, as `TaskLost`, once both limits and one more minute have passed.

   TaskIQ stops a task by cancelling it, which only works while the task awaits something. Blocking code, like `time.sleep()` or a long computation, can't be stopped, and holds up the worker's other tasks: use async libraries, or run it with `await asyncio.to_thread(...)`. In a thread, the task fails at `time_limit`, but the thread keeps running until it ends.
6. Add your own tasks in `taskiq_task.py`. The included `divide` task simulates a long-running operation:
    ```python
    @broker.task(task_name="divide")
    async def divide(x: int, y: int, user_id: str):
        """Divide x by y. It waits 5 seconds first, like a long-running task."""
        try:
            await asyncio.sleep(5)  # Simulate a long-running operation
            result: float = x / y
        except Exception as e:
            # The credit was reserved when the task was queued, and only
            # successful runs are charged: give it back.
            await Account.refund_credit(user_id)
            return {
                "status": "error",
                "message": "An unexpected error occurred",
                "error": str(e),
            }

        return result
    ```
   To add a task, write it in `taskiq_task.py`, as an async function with `@broker.task(task_name=...)`. For example, one that squares a number:
    ```python
    @broker.task(task_name="square")
    async def square(n: int, user_id: str):
        """Square n."""
        return n * n
    ```
   That's all: the panel lists every task in `taskiq_task.py`, with its docstring and an input for each parameter, and the API starts it at `/api/task/square`. The parameters are checked against their type hints before the task is queued. Besides them, every task gets `user_id`, the user's Google ID, which users can't set. The name is saved as the task's type in the history, so keep it under 50 characters and don't change it once the task has run. Users can start any task in `taskiq_task.py`, so don't put tasks there that they shouldn't run.

   The tasks can use the database through the models in `web/db_models.py`: the worker connects to it when it starts. When a task ends, the worker saves its status and result in the task history. If the task raises an exception, it ends as FAILURE and its credit is given back automatically; if it catches the error and returns a result instead, as `divide` does, call `Account.refund_credit(user_id)` yourself. If your tasks take longer than 10 minutes, raise `time_limit`.

   Return plain Python values (dicts, lists, numbers or strings): TaskIQ turns them into JSON. Don't return `json.dumps(...)`: the result would be saved as a string, and the API would show it with escaped quotes.
7. Customize the pages in `web/templates/`. Put images, icons and other static files in `web/static/`: they are served at `/web/static/`. In the templates, start their URLs with `{{ root_prefix }}` so they also work with a `ROOT_PATH`, as the logo in `login.html` does.

   The pages load Tailwind from `web/static/tailwind.css`, which only has the classes the templates use. After changing them, rebuild it with `python scripts/build_css.py`: it uses Tailwind's standalone CLI, which doesn't need Node.js and is downloaded the first time. The tests fail if a class in the templates is missing from it.

---

## Development Notes

- To monitor tasks, read the worker's log: it shows each task it runs, and the errors. With Docker, `docker compose logs -f worker`. The `taskhistory` table keeps every task's status and result.
- The database changes are migrations in `web/migrations/`, made with Tortoise ORM's own tool. The backend applies the new ones when it starts, and records them in the `tortoise_migrations` table. After changing the models in `web/db_models.py`, create a migration from the repository root, with the virtual environment activated (it reads `config.toml`, but doesn't connect to the database):
    ```bash
    tortoise -c shared.TORTOISE_ORM makemigrations
    ```
    Then restart the backend to apply it (with Docker, `docker compose up --build`). The tests fail if the models have changes without a migration.
- A database created before the migrations is only recorded as migrated the first time this version starts: its tables don't change. If it's from before API keys became unique, add their index by hand:
    ```sql
    ALTER TABLE account ADD CONSTRAINT account_api_key_key UNIQUE (api_key);
    ```
- A database created before API keys were hashed has them in plain text. Run `scripts/hash_api_keys.sql` once, with the app stopped, before starting this version; the keys keep working:
    ```bash
    psql -h your_db_host -p 5432 -U your_db_username -d your_database_name -f scripts/hash_api_keys.sql
    ```

---

## Tests

The tests need Docker running: they start PostgreSQL and Redis in containers with [testcontainers](https://testcontainers-python.readthedocs.io/), and run the backend and a TaskIQ worker as separate processes, like in production. Your `config.toml` and your database are not touched.

```bash
pip install -r requirements-dev.txt
pytest
```

They take a few minutes: the example task waits 5 seconds, and some tests wait for tasks to time out.

GitHub Actions runs them with Python 3.11 and 3.15 on every push to `main` and on every pull request (`.github/workflows/tests.yml`). It also starts the app with Docker Compose and runs two tasks through the API.

---

## Contributing

Contributions are welcome! Please fork the repository and submit a pull request. For major changes, please open an issue first to discuss your ideas.

---

## License

This project is licensed under the MIT License. See the `LICENSE` file for details.

---

## Acknowledgments

Built with ❤️ by [Carlos A. Planchón](https://github.com/carlosplanchon) in Dolores, Uruguay.
