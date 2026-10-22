// The user panel: its tasks, its credits and its history (web/templates/user_panel.html).

// The ROOT_PATH of the app's URLs, like "/nanosaas", from the page's <html>.
const ROOT_PREFIX = document.documentElement.dataset.rootPrefix;

// When the session expires, go back to the login page.
function sessionExpired(response) {
    if (response.status !== 401) {
        return false;
    }
    window.location.href = `${ROOT_PREFIX}/`;
    return true;
}

// A task's parameters, like "x: 6, y: 3".
function formatParameters(parameters) {
    return Object.entries(parameters ?? {}).map(([name, value]) => `${name}: ${JSON.stringify(value)}`).join(', ');
}

// What a task costs, like "Costs 3 credits, ...", before the task types load: nothing.
function formatCost(cost) {
    if (!cost) return '';
    return `Costs ${cost} credit${cost === 1 ? '' : 's'}, given back if it fails or is canceled.`;
}

// A parameter's input: a list to choose from, for an enum or a Literal,
// or else the one of its JSON schema type. Lists, and objects like dicts
// and models, are typed as JSON.
function inputKind(parameter) {
    if (parameter.options) return 'select';
    if (parameter.type === 'boolean') return 'checkbox';
    if (['array', 'object'].includes(parameter.type)) return 'json';
    if (['integer', 'number'].includes(parameter.type)) return 'number';
    return 'text';
}

// Text that isn't JSON goes as it is, and the server says what's wrong with it.
function parseJson(text) {
    try {
        return JSON.parse(text);
    } catch {
        return text;
    }
}

// What a parameter's input starts with: its default, if it has one, as
// JSON for lists, objects and choices (an option's value is JSON, so a
// number stays a number). A checkbox without one starts unchecked.
function initialValue(parameter) {
    const kind = inputKind(parameter);
    if (kind === 'checkbox') return parameter.default ?? false;
    if (parameter.default === null || parameter.default === undefined) return '';
    return ['json', 'select'].includes(kind) ? JSON.stringify(parameter.default) : parameter.default;
}

function taskManager() {
    let timer = null;

    return {
        taskTypes: [], // The tasks the user can start, from /user/tasks
        chosen: '',
        values: {},
        tasks: [],
        // The page of the history shown, as the task its tasks come before (null
        // for the first page), after the ones of the newer pages, to go back.
        pages: [null],
        hasMore: false, // Whether there are older tasks
        credits: 0, // Initialize credits
        // The task being started, and the Idempotency-Key it's sent with: sent
        // again before an answer, after a double click or a network error, it
        // gets the same task instead of starting and paying for another one.
        sending: null,

        init() {
            this.fetchTaskTypes();
            this.refresh();

            // Don't poll while the tab is hidden, and catch up when it shows again.
            document.addEventListener('visibilitychange', () => {
                if (document.hidden) {
                    clearTimeout(timer);
                } else {
                    this.refresh();
                }
            });
        },

        // Every 5 seconds while a task is pending, every 30 otherwise.
        async refresh() {
            // History first, so the credits count the refunds of the finished tasks it shows.
            await this.fetchTaskHistory();
            await this.fetchCredits();
            if (document.hidden) return;

            const busy = this.tasks.some(task => task.status === 'PENDING');
            clearTimeout(timer);
            timer = setTimeout(() => this.refresh(), busy ? 5000 : 30000);
        },

        async fetchCredits() {
            try {
                const response = await fetch(`${ROOT_PREFIX}/user/credits`);
                if (sessionExpired(response)) return;
                if (response.ok) {
                    const data = await response.json();
                    this.credits = data.credits;
                } else {
                    console.error('Failed to fetch credits');
                    this.credits = 'Error';
                }
            } catch (error) {
                console.error('Error fetching credits:', error);
                this.credits = 'Error';
            }
        },

        // The page shown, so a refresh shows the latest of its tasks.
        async fetchTaskHistory() {
            const before = this.pages.at(-1);
            const query = before ? `?before=${encodeURIComponent(before)}` : '';
            try {
                const response = await fetch(`${ROOT_PREFIX}/user/task_history${query}`);
                if (sessionExpired(response)) return;
                // Another page was chosen meanwhile: this one is not shown anymore.
                if (before !== this.pages.at(-1)) return;
                if (response.ok) {
                    const data = await response.json();
                    this.tasks = data.tasks.map(task => ({
                        ...task,
                        created_at: new Date(task.created_at).toLocaleString()
                    }));
                    this.hasMore = data.has_more;
                } else {
                    console.error('Failed to fetch task history');
                }
            } catch (error) {
                console.error('Error fetching task history:', error);
            }
        },

        // The tasks before the last one shown.
        olderTasks() {
            this.pages.push(this.tasks.at(-1).task_id);
            this.refresh();
        },

        newerTasks() {
            this.pages.pop();
            this.refresh();
        },

        chosenTask() {
            return this.taskTypes.find(taskType => taskType.name === this.chosen);
        },

        resetValues() {
            const parameters = this.chosenTask()?.parameters ?? [];
            this.values = Object.fromEntries(parameters.map(parameter => [parameter.name, initialValue(parameter)]));
        },

        async fetchTaskTypes() {
            try {
                const response = await fetch(`${ROOT_PREFIX}/user/tasks`);
                if (sessionExpired(response)) return;
                if (response.ok) {
                    this.taskTypes = await response.json();
                    this.chosen = this.taskTypes[0]?.name ?? '';
                    this.resetValues();
                } else {
                    console.error('Failed to fetch the tasks');
                }
            } catch (error) {
                console.error('Error fetching the tasks:', error);
            }
        },

        async startTask() {
            const taskType = this.chosenTask();
            if (!taskType) return;

            // Numbers go as numbers, lists, objects and choices as their JSON,
            // and empty inputs are left out, so the task's defaults apply.
            const parameters = {};
            for (const parameter of taskType.parameters) {
                let value = this.values[parameter.name];
                if (value === '' || value === null || value === undefined) continue;
                const kind = inputKind(parameter);
                if (kind === 'number') value = Number(value);
                if (['json', 'select'].includes(kind)) value = parseJson(value);
                parameters[parameter.name] = value;
            }

            // Another task or other parameters: a new key.
            const request = JSON.stringify([taskType.name, parameters]);
            if (this.sending?.request !== request) {
                this.sending = { request, key: crypto.randomUUID() };
            }

            try {
                const response = await fetch(`${ROOT_PREFIX}/task/${encodeURIComponent(taskType.name)}`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json', 'Idempotency-Key': this.sending.key },
                    body: JSON.stringify(parameters)
                });
                // Answered: starting it again starts another task.
                this.sending = null;
                if (sessionExpired(response)) return;

                if (response.ok) {
                    // Back to the first page, which has the new task.
                    this.pages = [null];
                    this.refresh(); // Refresh task history and credits after starting a new task
                } else if (response.status === 403) {
                    alert('Not enough credits');
                } else if (response.status === 413 || response.status === 429) {
                    // Parameters too large, or too many pending tasks: the server says its limit.
                    alert((await response.json()).detail);
                } else if (response.status === 422) {
                    const data = await response.json();
                    // Where the error is, like "numbers.1" for an item of a list.
                    alert('Check the parameters: ' + data.detail.map(error => `${error.loc.slice(1).join('.')}: ${error.msg}`).join(', '));
                } else {
                    alert('Failed to start task');
                }
            } catch (error) {
                console.error('Error starting task:', error);
                alert('An error occurred while starting the task.');
            }
        },

        // Its credit comes back. A worker may have started it meanwhile: then it can't.
        async cancelTask(task) {
            try {
                const response = await fetch(`${ROOT_PREFIX}/task/cancel/${task.task_id}`, { method: 'POST' });
                if (sessionExpired(response)) return;
                if (response.status === 409) {
                    alert('The task already started, so it cannot be canceled.');
                } else if (!response.ok) {
                    alert('Failed to cancel the task');
                }
            } catch (error) {
                console.error('Error canceling task:', error);
                alert('An error occurred while canceling the task.');
            }
            this.refresh();
        }
    };
}
