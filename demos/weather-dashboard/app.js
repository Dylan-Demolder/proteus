/**
 * Weather Dashboard — fetches real-time weather from wttr.in API.
 * Features: city input, preset buttons, history, error handling, loading states.
 */
(function() {
  'use strict';

  // ── DOM refs ──
  const $ = (sel) => document.querySelector(sel);
  const cityInput     = $('#cityInput');
  const getBtn        = $('#getWeatherBtn');
  const loading       = $('#loading');
  const errorEl       = $('#error');
  const errorMsg      = $('#errorMessage');
  const weatherResult = $('#weatherResult');
  const cityName      = $('#cityName');
  const countryFlag   = $('#countryFlag');
  const temperature   = $('#temperature');
  const feelsLike     = $('#feelsLike');
  const weatherDesc   = $('#weatherDesc');
  const windInfo      = $('#windInfo');
  const humidity      = $('#humidity');
  const visibility    = $('#visibility');
  const uvIndex       = $('#uvIndex');
  const pressure      = $('#pressure');
  const historyList   = $('#historyList');

  // ── State ──
  let history = JSON.parse(localStorage.getItem('weatherHistory') || '[]');

  // ── Utils ──
  function capitalize(str) {
    return str.replace(/\b\w/g, c => c.toUpperCase());
  }

  function show(el) { el.classList.remove('hidden'); }
  function hide(el) { el.classList.add('hidden'); }

  function setError(msg) {
    errorMsg.textContent = msg;
    hide(loading);
    show(errorEl);
  }

  // ── History ──
  function renderHistory() {
    historyList.innerHTML = '';
    history.forEach(city => {
      const li = document.createElement('li');
      li.textContent = city;
      li.addEventListener('click', () => {
        cityInput.value = city;
        fetchWeather(city);
      });
      historyList.appendChild(li);
    });
  }

  function addToHistory(city) {
    const c = capitalize(city.trim());
    if (!c) return;
    history = history.filter(h => h.toLowerCase() !== c.toLowerCase());
    history.unshift(c);
    if (history.length > 10) history = history.slice(0, 10);
    localStorage.setItem('weatherHistory', JSON.stringify(history));
    renderHistory();
  }

  // ── API call ──
  async function fetchWeather(city) {
    const q = city.trim();
    if (!q) return;

    hide(errorEl);
    hide(weatherResult);
    show(loading);

    try {
      const url = `https://wttr.in/${encodeURIComponent(q)}?format=j1`;
      const resp = await fetch(url);

      if (!resp.ok) {
        throw new Error(resp.status === 404 ? 'City not found' : `Server error (${resp.status})`);
      }

      const data = await resp.json();
      displayWeather(data, q);
      addToHistory(q);
    } catch (err) {
      if (err.message === 'Failed to fetch') {
        setError('Network error — check your connection');
      } else {
        setError(err.message);
      }
    }
  }

  // ── Render ──
  function displayWeather(data, query) {
    const curr = data.current_condition && data.current_condition[0];
    if (!curr) {
      setError('No weather data available for this location');
      return;
    }

    const nearby = data.nearest_area && data.nearest_area[0];
    const areaName = nearby ? capitalize(nearby.areaName[0].value) : capitalize(query);
    const country = nearby ? nearby.country[0].value : '';

    cityName.textContent = areaName;
    countryFlag.textContent = getFlag(country);

    temperature.textContent = `${curr.temp_C}°C`;
    feelsLike.textContent = `Feels like ${curr.FeelsLikeC}°C`;

    const desc = curr.weatherDesc && curr.weatherDesc[0];
    weatherDesc.textContent = desc ? desc.value : '--';

    const windDir = curr.winddir16Point || '--';
    const windKmph = curr.windspeedKmph || '--';
    windInfo.textContent = `Wind: ${windDir} ${windKmph} km/h`;

    humidity.textContent = `${curr.humidity || '--'}%`;
    visibility.textContent = curr.visibility ? `${curr.visibility} km` : '-- km';
    uvIndex.textContent = curr.uvIndex || '--';
    pressure.textContent = curr.pressure ? `${curr.pressure} hPa` : '-- hPa';

    hide(loading);
    show(weatherResult);
  }

  // ── Country flag emoji helper ──
  function getFlag(country) {
    const map = {
      'United Kingdom': '🇬🇧', 'United States': '🇺🇸', 'Japan': '🇯🇵',
      'France': '🇫🇷', 'Australia': '🇦🇺', 'United Arab Emirates': '🇦🇪',
      'Iceland': '🇮🇸', 'Germany': '🇩🇪', 'Italy': '🇮🇹',
      'Spain': '🇪🇸', 'Canada': '🇨🇦', 'Brazil': '🇧🇷',
      'India': '🇮🇳', 'China': '🇨🇳', 'Russia': '🇷🇺',
      'South Africa': '🇿🇦', 'Egypt': '🇪🇬', 'Singapore': '🇸🇬',
      'Thailand': '🇹🇭', 'South Korea': '🇰🇷', 'Netherlands': '🇳🇱',
    };
    return map[country] || '🌍';
  }

  // ── Events ──
  getBtn.addEventListener('click', () => fetchWeather(cityInput.value));

  cityInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') fetchWeather(cityInput.value);
  });

  document.querySelectorAll('.preset').forEach(btn => {
    btn.addEventListener('click', () => {
      const city = btn.dataset.city;
      cityInput.value = city;
      fetchWeather(city);
    });
  });

  // ── Init ──
  renderHistory();

  // Auto-fetch London on load
  fetchWeather('London');
})();