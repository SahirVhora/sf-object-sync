/**
 * sf_object_sync - Web UI JavaScript
 * Handles: settings modal, localStorage persistence, dynamic auth fields
 */

const STORAGE_KEY = 'sf_sync_settings';

// ── Field map: modal input id → hidden form field id ──────────────────────────
const FIELD_MAP = {
  auth_method:         'h_auth_method',
  source_url:          'h_source_url',
  source_user:         'h_source_user',
  source_password:     'h_source_password',
  source_client_id:    'h_source_client_id',
  source_client_secret:'h_source_client_secret',
  source_token_url:    'h_source_token_url',
  source_cert_path:    'h_source_cert_path',
  source_key_path:     'h_source_key_path',
  source_company_id:   'h_source_company_id',
  target_url:          'h_target_url',
  target_user:         'h_target_user',
  target_password:     'h_target_password',
  target_client_id:    'h_target_client_id',
  target_client_secret:'h_target_client_secret',
  target_token_url:    'h_target_token_url',
  target_cert_path:    'h_target_cert_path',
  target_key_path:     'h_target_key_path',
  target_company_id:   'h_target_company_id',
};

// ── Toggle auth-specific fields inside the modal ──────────────────────────────
function toggleAuthFields(method) {
  document.querySelectorAll('.auth-fields').forEach(el => {
    el.hidden = el.dataset.auth !== method;
  });
}

// ── Modal open / close ────────────────────────────────────────────────────────
function openSettings() {
  document.getElementById('settings-overlay').hidden = false;
  document.body.style.overflow = 'hidden';
}

function closeSettings() {
  document.getElementById('settings-overlay').hidden = true;
  document.body.style.overflow = '';
}

// Close on overlay backdrop click
document.addEventListener('DOMContentLoaded', function () {
  document.getElementById('settings-overlay').addEventListener('click', function (e) {
    if (e.target === this) closeSettings();
  });
});

// Close on Escape key
document.addEventListener('keydown', function (e) {
  if (e.key === 'Escape') closeSettings();
});

// ── Save settings to localStorage ────────────────────────────────────────────
function saveSettings() {
  const settings = {};
  Object.keys(FIELD_MAP).forEach(function (id) {
    const el = document.getElementById(id);
    if (el) settings[id] = el.value;
  });
  localStorage.setItem(STORAGE_KEY, JSON.stringify(settings));

  // Update credentials banner on main page
  updateCredentialsBanner(settings);

  // Close modal after a brief flash of the saved indicator
  const indicator = document.getElementById('settings-saved-indicator');
  indicator.hidden = false;
  setTimeout(function () {
    closeSettings();
  }, 600);
}

// ── Clear saved settings ──────────────────────────────────────────────────────
function clearSettings() {
  localStorage.removeItem(STORAGE_KEY);

  // Clear modal fields
  Object.keys(FIELD_MAP).forEach(function (id) {
    const el = document.getElementById(id);
    if (el) el.value = '';
  });

  // Reset auth method to basic
  const authSelect = document.getElementById('auth_method');
  if (authSelect) {
    authSelect.value = 'basic';
    toggleAuthFields('basic');
  }

  // Hide indicators
  const indicator = document.getElementById('settings-saved-indicator');
  if (indicator) indicator.hidden = true;
  const banner = document.getElementById('credentials-banner');
  if (banner) banner.hidden = true;
}

// ── Load settings from localStorage and populate modal ───────────────────────
function loadSettings() {
  const raw = localStorage.getItem(STORAGE_KEY);
  if (!raw) return null;

  let settings;
  try {
    settings = JSON.parse(raw);
  } catch (e) {
    return null;
  }

  Object.keys(FIELD_MAP).forEach(function (id) {
    const el = document.getElementById(id);
    if (el && settings[id] !== undefined) el.value = settings[id];
  });

  // Apply auth method toggle
  const method = settings['auth_method'] || 'basic';
  const authSelect = document.getElementById('auth_method');
  if (authSelect) {
    authSelect.value = method;
    toggleAuthFields(method);
  }

  // Show saved indicator in modal
  const indicator = document.getElementById('settings-saved-indicator');
  if (indicator) indicator.hidden = false;

  return settings;
}

// ── Show/hide credentials banner on main page ─────────────────────────────────
function updateCredentialsBanner(settings) {
  const banner = document.getElementById('credentials-banner');
  if (!banner) return;
  const hasUrl = settings && (settings.source_url || settings.target_url);
  banner.hidden = !hasUrl;
}

// ── Populate hidden form fields from localStorage before submission ────────────
function populateHiddenFields() {
  const raw = localStorage.getItem(STORAGE_KEY);
  if (!raw) return;

  let settings;
  try {
    settings = JSON.parse(raw);
  } catch (e) {
    return;
  }

  Object.entries(FIELD_MAP).forEach(function ([modalId, hiddenId]) {
    const hidden = document.getElementById(hiddenId);
    if (hidden && settings[modalId] !== undefined) {
      hidden.value = settings[modalId];
    }
  });
}

// ── Init on page load ─────────────────────────────────────────────────────────
window.addEventListener('DOMContentLoaded', function () {
  // Set initial auth field visibility (default: basic)
  toggleAuthFields('basic');

  // Load saved settings and update UI
  const settings = loadSettings();
  updateCredentialsBanner(settings);
});
