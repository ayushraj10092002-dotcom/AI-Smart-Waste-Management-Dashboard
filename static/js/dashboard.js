const clock = document.getElementById('live-clock');

if (clock) {
    const updateClock = () => {
        const locale = window.WASTE_LOCALE === 'hi' ? 'hi-IN' : 'en';
        const timeZone = window.WASTE_TIME_ZONE || 'UTC';
        const now = new Date();
        const dateParts = new Intl.DateTimeFormat('en', {
            year: 'numeric', month: '2-digit', day: '2-digit', timeZone,
        }).formatToParts(now);
        const dateValues = Object.fromEntries(dateParts.map((part) => [part.type, part.value]));
        const dateFormat = window.WASTE_DATE_FORMAT || '%Y-%m-%d';
        const date = dateFormat.replace('%Y', dateValues.year).replace('%m', dateValues.month).replace('%d', dateValues.day);
        const time = new Intl.DateTimeFormat(locale, {
            hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23',
            timeZone: window.WASTE_TIME_ZONE || 'UTC',
        }).format(now);
        clock.textContent = `${date} ${time}`;
    };
    updateClock();
    window.setInterval(updateClock, 1000);
}

async function renderWasteCharts() {
    const canvases = ['wasteChart', 'lineChart', 'pieChart']
        .map((id) => document.getElementById(id))
        .filter(Boolean);
    if (!canvases.length) return;

    try {
        const response = await fetch(window.WASTE_API.analytics);
        if (!response.ok) throw new Error('Analytics request failed');
        const analytics = await response.json();
        const labels = analytics.daily_waste.map((row) => row.day);
        const unit = window.WASTE_UNIT || 'kg';
        const conversion = unit === 'tonnes' ? 0.001 : unit === 'lbs' ? 2.2046226218 : 1;
        const values = analytics.daily_waste.map((row) => row.waste_kg * conversion);

        if (typeof window.Chart !== 'function') throw new Error('Chart.js could not be loaded');
        const palette = ['#187a58', '#d48a31', '#3684a0'];
        const common = { responsive: true, maintainAspectRatio: false };
        new Chart(document.getElementById('wasteChart'), {
            type: 'bar',
            data: { labels, datasets: [{ label: `Waste (${unit})`, data: values, backgroundColor: '#187a58', borderRadius: 4 }] },
            options: { ...common, plugins: { legend: { display: false } }, scales: { y: { beginAtZero: true, title: { display: true, text: unit } } } },
        });
        new Chart(document.getElementById('lineChart'), {
            type: 'bar',
            data: { labels: window.WASTE_LOCATIONS.map((item) => item.name), datasets: [{ label: `Simulated waste (${unit})`, data: window.WASTE_LOCATIONS.map((item) => item.waste_kg * conversion), backgroundColor: '#d48a31', borderRadius: 4 }] },
            options: { ...common, plugins: { legend: { display: false } }, scales: { y: { beginAtZero: true, title: { display: true, text: `${unit} · simulated` } } } },
        });
        new Chart(document.getElementById('pieChart'), {
            type: 'doughnut',
            data: { labels: Object.keys(window.BIN_STATUS_COUNTS), datasets: [{ data: Object.values(window.BIN_STATUS_COUNTS), backgroundColor: palette }] },
            options: { ...common, cutout: '58%', plugins: { legend: { position: 'bottom' } } },
        });
    } catch (error) {
        canvases.forEach((canvas) => {
            const message = document.createElement('p');
            message.className = 'notice notice-warn';
            message.textContent = 'Charts are unavailable. Check the network connection and reload the page.';
            canvas.replaceWith(message);
        });
        console.error('Unable to render waste charts:', error);
    }
}

renderWasteCharts();
