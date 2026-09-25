'use strict';
document.querySelector('#appearance-form').onsubmit = async event => {
  event.preventDefault();
  const form = event.target;
  const data = Object.fromEntries(new FormData(form));
  data.dashboard_compact = form.elements.dashboard_compact.checked;
  try {
    const response = await fetch('/api/suite/appearance', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(data)});
    if (!response.ok) throw new Error('Could not save appearance. Check the name and color.');
    location.reload();
  } catch (error) { document.querySelector('#appearance-result').textContent = error.message; }
};
