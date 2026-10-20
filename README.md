# NanoSaaS

NanoSaaS is a lightweight microservice platform for managing and tracking computational tasks. Write a task as an async Python function, and your users get a form to start it, an API route with its docs, and a history of its results, paying a credit only when it succeeds. Designed with simplicity and scalability in mind, NanoSaaS is perfect for developers and small teams looking to automate background processes efficiently.

I built the first version in three days, with almost no prior knowledge of web design or frontend development. Since then I've kept working on it: credits can't be overspent by concurrent requests, a task runs in one worker at a time, runs again if that worker dies, and gives its credit back if it fails, API keys are stored hashed, and the database has migrations. Integration tests run it in CI with PostgreSQL, Redis and real workers. What it doesn't do yet is under [Known Limitations](#known-limitations).

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
- **Frontend**: Alpine.js, Tailwind CSS
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
    The backend creates the tables, and applies new migrations, when it starts. To run Redis in Docker: `docker run -p 127.0.0.1:6379:6379 redis`. Binding the port to `127.0.0.1` keeps Redis reachable only from this machine: ports published by Docker skip firewalls like ufw. For the queued tasks to survive a crash of Redis, turn on its append-only file, as `docs/redis_conf.txt` explains.

3. Start the backend:
    ```bash
    uvicorn main_api:app --reload
    ```

4. Start the TaskIQ worker in another terminal, with the virtual environment activated:
    ```bash
    taskiq worker taskiq_task:broker --receiver taskiq_task:Receiver
    ```
    `--receiver` makes it run one copy of each task at a time, renewing its lease while it runs, and drop the tasks that waited in the queue longer than `queue_timeout`.

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

Any Google account can log in, and each new account gets `INITIAL_CREDITS` credits, so on a public server anyone can use them, even with several accounts. To choose who runs tasks, set `INITIAL_CREDITS = 0` in `config.toml` and give credits from the admin page or with `scripts/add_credits.py` (see Usage), once the user has logged in.

---

## Usage

1. Access the application at `http://127.0.0.1:8000`.
2. Log in using Google SSO. Logging out ends the user's sessions, on every device: their cookies stop working, even a copy of one.

   ![Login Page](docs/login.png)

3. Create and manage tasks via the user dashboard: choose a task, fill in its parameters and start it.

   ![User Panel](docs/user_panel.png)

   It shows the latest 50 tasks (`TASK_HISTORY_LIMIT` in `main_api.py`). It checks them every 5 seconds while one is pending, every 30 seconds otherwise, and not while its tab is hidden.

   At the bottom of the panel, users can delete their account, with its tasks and its API key: its sessions end on every device. All that's kept is a hash of its Google ID, so that logging in with it again starts an account without `INITIAL_CREDITS`: otherwise, deleting the account would be a way to get them again. The results of its tasks may stay in Redis for up to a day, until the result backend forgets them.

4. Use the API with the key from the *API Key Management* page, sent in the `X-API-Key` header. A task starts with a POST to `/api/task/<name>`, with its parameters in a JSON object:
    ```bash
    curl -H "X-API-Key: your_api_key" http://127.0.0.1:8000/api/user_credits
    curl -H "X-API-Key: your_api_key" -H "Content-Type: application/json" -d '{"x": 10, "y": 4}' http://127.0.0.1:8000/api/task/divide
    curl -H "X-API-Key: your_api_key" http://127.0.0.1:8000/api/task_details/your_task_id
    ```
   Wrong parameters get a 422 error, without spending a credit. The API key also works in the `api_key` query parameter, for older clients, but avoid it: URLs end up in server and proxy logs. The interactive docs at `/docs` list every route, and under **tasks**, every task with its description and parameters, where you can also try them.

   To retry a request safely, for example after a timeout, send it with an `Idempotency-Key` header, with a value you pick for each request, like a UUID. Sent again with the same key, the request gets the task it started, without paying for another one, and if it didn't start one, it starts it then. Reusing a key for another task or other parameters gets a 422 error. The panel does the same, so a double click starts one task.

   The page shows the whole key only when you generate it: the database keeps just its first characters and a hash of it.
5. Each user starts with `INITIAL_CREDITS` credits, set in `config.toml` (25 by default). A credit is reserved when a task is queued and given back if the task fails, so only successful tasks are charged. When credits run out, the user can't start more tasks until you add more, giving the email the user logs in with:
    ```bash
    docker compose exec api python scripts/add_credits.py user@example.com 25
    ```
   Without Docker, run `python scripts/add_credits.py user@example.com 25` from the repository root. To look into the database itself, `docker compose exec postgres psql -U nanosaas` opens a shell in it.

   You can also add credits from the admin page, `/admin`, after putting your email in `ADMINS`, in `config.toml`: the user panel then shows you a button to it. It lists the latest accounts, which you can search by email, with their credits and pending tasks. For any other user, it doesn't exist.

   A task that waits in the queue longer than `queue_timeout` seconds is dropped, failing as `TaskExpired`, and one that runs longer than `time_limit` seconds is stopped, failing as `TimeoutError` (10 minutes each by default, set in `config.toml`): either way its credit comes back.

   A task whose worker dies runs again, in another worker. While a worker runs a task, it renews the task's lease every few seconds, and the workers queue again the tasks whose lease went `lease` seconds (30 by default) without renewal, up to `attempts` runs in all (3 by default): after the last one, the task fails as `TaskLost`, and its credit comes back. A task runs again from the start, so make the tasks that change things outside NanoSaaS safe to run twice: for example, with their `task_id` as the idempotency key of the services they call (see below). A task that never ends otherwise, for example because Redis lost it, also fails as `TaskLost`: one that never started, once it could have waited in the queue (`queue_timeout` and one more minute), and one that did, once each of its runs could have waited in the queue and run until stopped (about an hour by default).

   Only one worker runs a task at a time. When Redis queues a task but its reply to the backend is lost, redis-py sends the task again, and when there's no reply at all, the backend gives the task up and its credit back, though Redis may have queued it. So the worker marks each task as started in the task history before running it, and skips the copies of a task that already started, and the tasks given up.

   TaskIQ stops a task by cancelling it, which only works while the task awaits something. Blocking code, like `time.sleep()` or a long computation, can't be stopped, holds up the worker's other tasks, and keeps the worker from renewing the task's lease, so after `lease` seconds the task also runs in another worker: use async libraries, or run it with `await asyncio.to_thread(...)`. In a thread, the task fails at `time_limit`, but the thread keeps running until it ends.
6. Add your own tasks in `taskiq_task.py`. The included `divide` task simulates a long-running operation:
    ```python
    @broker.task(task_name="divide")
    async def divide(x: int, y: int, user_id: str):
        """Divide x by y. It waits 5 seconds first, like a long-running task."""
        await asyncio.sleep(5)  # Simulate a long-running operation
        if y == 0:
            # It fails, so its credit comes back, and the user sees this message.
            raise TaskError("Can't divide by zero.")
        return x / y
    ```
   To add a task, write it in `taskiq_task.py`, as an async function with `@broker.task(task_name=...)`. For example, one that squares a number:
    ```python
    @broker.task(task_name="square")
    async def square(n: int, user_id: str):
        """Square n."""
        return n * n
    ```
   That's all: the panel lists every task in `taskiq_task.py`, with its docstring and an input for each parameter (a checkbox for a `bool`, a list to choose from for an `Enum` or a `Literal`, and JSON for lists, dicts and Pydantic models), and the API starts it at `/api/task/square`, which `/docs` shows too. The parameters are checked against their type hints before the task is queued. Besides them, every task gets `user_id`, the user's Google ID, and a task with a `task_id` parameter gets its ID, which is the same in each of its runs: users can set neither. The name is saved as the task's type in the history, so keep it under 50 characters and don't change it once the task has run. Users can start any task in `taskiq_task.py`, so don't put tasks there that they shouldn't run.

   The tasks can use the database through the models in `web/db_models.py`: the worker connects to it when it starts. When a task ends, the worker saves its status and result in the task history. If the task raises an exception, it ends as FAILURE and its credit is given back. Only the exception's type is saved, since its message may expose details of your servers: to tell the user why the task failed, raise `TaskError` with a message, as `divide` does, and the message is saved too. Don't catch an error to return it as the result instead: the task would end as SUCCESS, and be charged. If your tasks take longer than 10 minutes, raise `time_limit`.

   A task whose worker dies runs again from the start, so if it changes things outside NanoSaaS, pass its `task_id` to the services it calls as their idempotency key, where they take one: they do it once, however many times the task runs. For example, with [httpx](https://www.python-httpx.org/):
    ```python
    @broker.task(task_name="send_message")
    async def send_message(to: str, text: str, user_id: str, task_id: str):
        """Send a message."""
        async with httpx.AsyncClient() as client:
            await client.post(
                "https://api.example.com/messages",
                json={"to": to, "text": text},
                headers={"Idempotency-Key": task_id},
            )
    ```

   Return plain Python values (dicts, lists, numbers or strings): TaskIQ turns them into JSON. Don't return `json.dumps(...)`: the result would be saved as a string, and the API would show it with escaped quotes.
7. Customize the pages in `web/templates/`. Put images, icons and other static files in `web/static/`: they are served at `/web/static/`. In the templates, start their URLs with `{{ root_prefix }}` so they also work with a `ROOT_PATH`, as the logo in `login.html` does.

   The pages load Tailwind from `web/static/tailwind.css`, which only has the classes used in the templates and in the scripts of `web/static/`, like the colors of the results' JSON in `json.js`. After changing them, rebuild it with `python scripts/build_css.py`: it uses Tailwind's standalone CLI, which doesn't need Node.js and is downloaded the first time. The tests fail if one of their classes is missing from it.

   Alpine.js is in `web/static/` too, next to its license, so the pages load nothing from other sites: they work without them, and no CDN sees who visits them. To update it, save the new version's `dist/cdn.min.js` there as `alpine-<version>.min.js`, for example with `curl -o web/static/alpine-X.Y.Z.min.js https://cdn.jsdelivr.net/npm/alpinejs@X.Y.Z/dist/cdn.min.js`, delete the old file, and change the version in the templates that load it. With the version in the file's name, browsers don't keep using the old one from their cache.

---

## Development Notes

- To monitor tasks, read the worker's log: it shows each task it runs, and the errors, with their traceback, except a `TaskError`, which takes a line. With Docker, `docker compose logs -f worker`. The `taskhistory` table keeps every task's status and result.
- The database changes are migrations in `web/migrations/`, made with Tortoise ORM's own tool. The backend applies the new ones when it starts, and records them in the `tortoise_migrations` table. After changing the models in `web/db_models.py`, create a migration from the repository root, with the virtual environment activated (it reads `config.toml`, but doesn't connect to the database):
    ```bash
    tortoise -c shared.TORTOISE_ORM makemigrations
    ```
    Then restart the backend to apply it (with Docker, `docker compose up --build`). The tests fail if the models have changes without a migration.
- A database created before the migrations already has the tables of the first one: the first time this version starts, that migration is only recorded as applied, and the later ones are applied as usual. If it's from before API keys became unique, add their index by hand:
    ```sql
    ALTER TABLE account ADD CONSTRAINT account_api_key_key UNIQUE (api_key);
    ```
- A Redis started with an older `compose.yaml` only saved snapshots, and this one starts it empty: the tasks waiting in its queue would be lost, and fail as `TaskLost`. To keep them, turn on its append-only file while it runs, before you update the app:
    ```bash
    docker compose exec redis redis-cli config set appendonly yes
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

GitHub Actions runs them with Python 3.11 and 3.15 on every push to `main` and on every pull request (`.github/workflows/tests.yml`). It also starts the app with Docker Compose and runs two tasks through the API. Every week, Dependabot opens pull requests with the new versions of the Python packages and of the workflow's actions, which are pinned (`.github/dependabot.yml`), and the tests run on them. It doesn't watch Alpine.js, in `web/static/`: update it by hand, as step 7 of Usage says.

---

## Known Limitations

- A task whose worker dies in the middle of it runs again from the start, so a task that changes things outside NanoSaaS, like sending an email, may do it twice, unless the service it calls takes its `task_id` as an idempotency key.
- There are no payments: users get `INITIAL_CREDITS` the first time they log in, and more only when you add them, from the admin page or with `scripts/add_credits.py`.

---

## Contributing

Contributions are welcome! Please fork the repository and submit a pull request. For major changes, please open an issue first to discuss your ideas.

---

## License

This project is licensed under the MIT License. See the `LICENSE` file for details.

---

## Acknowledgments

Built with ❤️ by [Carlos A. Planchón](https://github.com/carlosplanchon) in Dolores, Uruguay.
