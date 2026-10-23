// The details of a task, until it ends (web/templates/task_details.html).

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

function taskDetails() {
    return {
        task: {
            task_id: '',
            task_type: '',
            parameters: null,
            status: '',
            cost: '',
            result: '',
            created_at: ''
        },

        // The task's ID is in the page, in its element's data-task-id.
        init() {
            this.fetchTaskDetails(this.$el.dataset.taskId);
        },

        // For the template, whose expressions can only use the component's data and
        // methods: Alpine's CSP build doesn't run any other JavaScript in them.
        parametersText() {
            return JSON.stringify(this.task.parameters, null, 2);
        },

        // The result in the element: its JSON, with its colors, once the task ended,
        // as SUCCESS or FAILURE, and a dash before, or if it was canceled. A task
        // that returned None has null.
        showResult(element, task) {
            if (['SUCCESS', 'FAILURE'].includes(task.status)) {
                element.innerHTML = highlightJson(task.result);
            } else {
                element.textContent = '—';
            }
        },

        fetchTaskDetails(taskId) {
            const updateInterval = 5000; // 5 seconds
            let timer = null;
            // The status is empty until the first answer, and PENDING until the task ends.
            const finished = () => !['', 'PENDING'].includes(this.task.status);
            const fetchDetails = () => {
                fetch(`${ROOT_PREFIX}/api/task_details_from_user/${taskId}`)
                    .then(response => {
                        if (sessionExpired(response)) {
                            throw new Error('Session expired');
                        }
                        if (!response.ok) {
                            throw new Error(`HTTP error! Status: ${response.status}`);
                        }
                        return response.json();
                    })
                    .then(data => {
                        this.task = data;
                    })
                    .catch(error => console.error('Error fetching task details:', error))
                    .finally(() => {
                        clearTimeout(timer);
                        if (!finished() && !document.hidden) {
                            timer = setTimeout(fetchDetails, updateInterval);
                        }
                    });
            };

            // Don't poll while the tab is hidden, and catch up when it shows again.
            document.addEventListener('visibilitychange', () => {
                clearTimeout(timer);
                if (!document.hidden && !finished()) {
                    fetchDetails();
                }
            });

            // Fetch immediately, and again every 5 seconds until the task ends
            fetchDetails();
        }
    };
}

// Alpine.js, which loads after this script, finds the component by its name.
document.addEventListener('alpine:init', () => {
    Alpine.data('taskDetails', taskDetails);
});
