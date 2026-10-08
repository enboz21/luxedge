(function (root) {
    function createPoller(isVisible, schedule = setInterval, cancel = clearInterval) {
        const jobs = new Map();
        function add(name, action, interval) {
            const job = { action, interval, timer: null, pending: false };
            jobs.set(name, job);
            return async function run() {
                if (!isVisible() || job.pending) return;
                job.pending = true;
                try { return await action(); }
                finally { job.pending = false; }
            };
        }
        function refresh() {
            for (const job of jobs.values()) {
                if (!isVisible()) {
                    if (job.timer !== null) cancel(job.timer);
                    job.timer = null;
                } else if (job.timer === null) {
                    const run = async () => {
                        if (!isVisible() || job.pending) return;
                        job.pending = true;
                        try { await job.action(); }
                        finally { job.pending = false; }
                    };
                    job.timer = schedule(run, job.interval);
                    void run();
                }
            }
        }
        return { add, refresh };
    }
    if (typeof module !== 'undefined') module.exports = { createPoller };
    else root.createPoller = createPoller;
})(globalThis);
