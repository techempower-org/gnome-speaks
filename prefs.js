// SPDX-License-Identifier: GPL-3.0-or-later
// GNOME Speaks — TTS/STT floating badge for GNOME Shell
// Copyright (C) 2025 JP Hein
//
// This program is free software: you can redistribute it and/or modify
// it under the terms of the GNU General Public License as published by
// the Free Software Foundation, either version 3 of the License, or
// (at your option) any later version.
//
// Preferences window — task-first IA (see docs/superpowers/specs/
// 2026-08-12-prefs-redesign.md). Rules that shape every row here:
//   - Explain on the row (subtitles), not in group prose — libadwaita's
//     preferences search matches row subtitles but NEVER group
//     descriptions, so prose paragraphs are invisible to search.
//   - Money and destruction are visible: banners + warning subtitles.
//   - Defaults are stated in subtitles; no subsystem jargon in titles.

import Adw from 'gi://Adw';
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import Gtk from 'gi://Gtk';
import {ExtensionPreferences} from 'resource:///org/gnome/Shell/Extensions/js/extensions/prefs.js';

// ─── Azure Speech Service regions ────────────────────────────────────────────
const REGIONS = [
    ['eastus', 'East US'],
    ['eastus2', 'East US 2'],
    ['westus', 'West US'],
    ['westus2', 'West US 2'],
    ['westus3', 'West US 3'],
    ['centralus', 'Central US'],
    ['northcentralus', 'North Central US'],
    ['southcentralus', 'South Central US'],
    ['canadacentral', 'Canada Central'],
    ['northeurope', 'North Europe (Ireland)'],
    ['westeurope', 'West Europe (Netherlands)'],
    ['uksouth', 'UK South'],
    ['francecentral', 'France Central'],
    ['germanywestcentral', 'Germany West Central'],
    ['swedencentral', 'Sweden Central'],
    ['switzerlandnorth', 'Switzerland North'],
    ['norwayeast', 'Norway East'],
    ['eastasia', 'East Asia (Hong Kong)'],
    ['southeastasia', 'Southeast Asia (Singapore)'],
    ['japaneast', 'Japan East'],
    ['japanwest', 'Japan West'],
    ['koreacentral', 'Korea Central'],
    ['australiaeast', 'Australia East'],
    ['centralindia', 'Central India'],
    ['brazilsouth', 'Brazil South'],
    ['uaenorth', 'UAE North'],
    ['southafricanorth', 'South Africa North'],
];

const SUBTITLE_COLORS = [
    ['default', 'Default'],
    ['green', 'Green'],
    ['light_green', 'Light Green'],
    ['yellow', 'Yellow'],
    ['amber', 'Amber'],
    ['rust', 'Rust'],
    ['red', 'Red'],
    ['light_red', 'Light Red'],
    ['blue', 'Blue'],
    ['light_blue', 'Light Blue'],
    ['cyan', 'Cyan'],
    ['light_cyan', 'Light Cyan'],
    ['magenta', 'Magenta'],
    ['light_magenta', 'Light Magenta'],
    ['white', 'White'],
    ['gray', 'Gray'],
];

const HD_VOICES = [
    'Ava', 'Andrew', 'Brian', 'Emma', 'Aria', 'Davis', 'Jenny', 'Guy',
    'Steffan', 'Christopher', 'Eric', 'Roger', 'Alloy', 'Echo', 'Fable',
    'Onyx', 'Nova', 'Shimmer',
].map(n => [`en-US-${n}:DragonHDLatestNeural`, `${n} (DragonHD)`]);

const FAST_VOICES = [
    'Ava', 'Andrew', 'Aria', 'Davis', 'Jenny', 'Guy', 'Brian', 'Emma',
    'Steffan', 'Christopher', 'Eric', 'Roger', 'Michelle', 'Monica',
    'Cora', 'Jane', 'Nancy', 'Sara', 'Tony', 'Jason', 'Brandon', 'Jacob',
    'Amber', 'Ashley', 'Elizabeth',
].map(n => [`en-US-${n}Neural`, n]);

// ─── Main Preferences Class ─────────────────────────────────────────────────
export default class GnomeSpeaksPreferences extends ExtensionPreferences {
    fillPreferencesWindow(window) {
        this._config = this._loadConfig();
        this._ccaConfig = this._loadCcaConfig();
        this._settings = this.getSettings();
        this._saveTimeoutId = null;
        this._ccaSaveTimeoutId = null;

        window.set_default_size(720, 860);
        window.set_search_enabled(true);

        window.connect('close-request', () => {
            this._flushConfigSave();
            this._flushCcaConfigSave();
            return false;
        });

        this._addDictationPage(window);
        this._addAiChatPage(window);
        this._addVoiceSoundPage(window);
        this._addWakeSpellsPage(window);
        this._addAccountsPage(window);
        this._addAdvancedPage(window);
    }

    // ═══════════════════════════════════════════════════════════════════
    // Page 1 — Dictation: speaking text into the computer
    // ═══════════════════════════════════════════════════════════════════
    _addDictationPage(window) {
        const page = new Adw.PreferencesPage({
            title: 'Dictation',
            icon_name: 'input-keyboard-symbolic',
        });

        // ── Typing ──
        const typeGroup = new Adw.PreferencesGroup({title: 'Typing'});
        page.add(typeGroup);

        this._addSwitchRow(typeGroup, 'Type at Cursor',
            'Type transcribed words where the cursor is (via ydotool). Off = copy to clipboard only. Default: on',
            'dictation_mode', true);

        this._addSwitchRow(typeGroup, 'Keep Live Text',
            'Keep the live-typed words as-is instead of replacing them with the final corrected transcript. Default: on',
            'skip_final_paste', true);

        this._addSwitchRow(typeGroup, 'Terminal Style',
            'All lowercase, no punctuation — for code and terminal input. Default: off',
            'terminal_mode', false);

        this._addSwitchRow(typeGroup, 'Spoken Punctuation',
            'Turn "period", "comma", "new line" into characters. Default: on',
            'voice_commands', true);

        this._addEntryRow(typeGroup, 'Stop Word', 'end_word', 'over',
            'Say this word to stop recording immediately. Default: over');

        // ── Flow — every listening timeout lives here ──
        const flowGroup = new Adw.PreferencesGroup({title: 'Flow'});
        page.add(flowGroup);

        this._addSwitchRow(flowGroup, 'Continuous Dictation',
            'Listen again automatically after each pause — dictate without re-clicking. Default: off',
            'continuous_dictation', false);

        this._addSpinRow(flowGroup, 'Pause Before Stop', 'silence_timeout',
            0.5, 10.0, 0.5, 1, 3.0,
            'Seconds of silence after speech before recording stops. Default: 3');

        this._addSpinRow(flowGroup, 'Give Up After', 'no_speech_timeout',
            1.0, 30.0, 1.0, 0, 7.0,
            'Seconds to wait for any speech before stopping. Default: 7');

        this._addSpinRow(flowGroup, 'Continuous Pause', 'loop_silence_timeout',
            0.3, 5.0, 0.1, 1, 1.2,
            'Shorter pause detection while in continuous mode. Default: 1.2');

        this._addSpinRow(flowGroup, 'Recording Limit', 'max_record_seconds',
            5, 300, 5, 0, 120,
            'Absolute maximum seconds for one recording. Default: 120');

        // ── Hearing ──
        const hearGroup = new Adw.PreferencesGroup({title: 'Hearing'});
        page.add(hearGroup);

        this._addSpinRow(hearGroup, 'Microphone Sensitivity', 'energy_multiplier',
            0.5, 20.0, 0.5, 1, 2.5,
            'Noise gate threshold — lower hears quieter speech, higher rejects more noise. Default: 2.5');

        this._addEntryRow(hearGroup, 'Language', 'language', 'en-US',
            'Speech recognition language code, e.g. en-US, de-DE, ja-JP. Default: en-US');

        // ── Starting — badge, panel, shortcuts ──
        const startGroup = new Adw.PreferencesGroup({title: 'Starting'});
        page.add(startGroup);

        this._addGSettingsSwitchRow(startGroup, 'Floating Badge',
            'Show the voice badge on the desktop — click it to start listening',
            'show-badge');

        this._addGSettingsSwitchRow(startGroup, 'Panel Indicator',
            'Show GNOME Speaks in the top panel',
            'show-panel-indicator');

        this._addGSettingsSpinRow(startGroup, 'Badge Position X', 'badge-position-x',
            -1, 5000, 1, 0, '-1 centers automatically');

        this._addGSettingsSpinRow(startGroup, 'Badge Position Y', 'badge-position-y',
            -1, 5000, 1, 0, '-1 sits at the bottom automatically');

        const shortcutGroup = new Adw.PreferencesGroup({title: 'Keyboard Shortcuts'});
        page.add(shortcutGroup);

        this._addShortcutRow(shortcutGroup, 'Toggle Listening', 'toggle-listening-shortcut');
        this._addShortcutRow(shortcutGroup, 'Speak Clipboard', 'speak-clipboard-shortcut');
        this._addShortcutRow(shortcutGroup, 'Read Selection', 'read-selection-shortcut');
        this._addShortcutRow(shortcutGroup, 'Toggle Voice Quality', 'toggle-voice-quality-shortcut');

        window.add(page);
    }

    // ═══════════════════════════════════════════════════════════════════
    // Page 2 — AI Chat: talking WITH the computer
    // ═══════════════════════════════════════════════════════════════════
    _addAiChatPage(window) {
        const page = new Adw.PreferencesPage({
            title: 'AI Chat',
            icon_name: 'user-available-symbolic',
        });

        // ── Conversation ──
        const convGroup = new Adw.PreferencesGroup({title: 'Conversation'});
        page.add(convGroup);

        this._addSwitchRow(convGroup, 'AI Conversation',
            'Send what you say to an AI and speak its reply aloud. Default: off',
            'conversation_mode', false);

        this._addComboRow(convGroup, 'Provider', 'llm_provider', [
            ['local', 'Local (OpenAI-compatible)'],
            ['anthropic', 'Anthropic (Claude)'],
            ['openai', 'OpenAI (GPT)'],
            ['digitalocean', 'DigitalOcean'],
            ['puter', 'Puter (Free)'],
            ['azure', 'Azure AI Foundry'],
            ['bedrock', 'AWS Bedrock'],
            ['google', 'Google Vertex AI'],
            ['cloud-chat-assistant', 'Cloud Chat Assistant'],
        ], 'anthropic');

        this._addComboRow(convGroup, 'Model', 'llm_model', [
            ['qwen36-coder', 'Qwen (local)'],
            ['claude-opus-4.6', 'Claude Opus 4.6'],
            ['claude-sonnet-4.6', 'Claude Sonnet 4.6'],
            ['claude-haiku-4.5', 'Claude Haiku 4.5'],
            ['claude-opus-4.5', 'Claude Opus 4.5'],
            ['claude-sonnet-4.5', 'Claude Sonnet 4.5'],
            ['gpt-4o', 'GPT-4o'],
            ['gpt-4o-mini', 'GPT-4o Mini'],
            ['o4-mini', 'o4-mini'],
            ['gpt-5.3', 'GPT-5.3'],
            ['llama-3.3-70b', 'Llama 3.3 70B'],
            ['deepseek-r1', 'DeepSeek R1'],
            ['grok-3', 'Grok-3'],
            ['gemini-2.5-flash', 'Gemini 2.5 Flash'],
            ['gemini-2.5-pro', 'Gemini 2.5 Pro'],
        ], 'claude-opus-4.6');

        this._addPasswordRow(convGroup, 'API Key (Chat AI)', 'llm_api_key',
            'Key for the selected provider — cloud providers bill per token');

        this._addSwitchRow(convGroup, 'Deep Thought',
            'Let local reasoning models think before answering — deeper replies, much slower first word. "cast deep thought" toggles by voice. Default: off',
            'llm_thinking', false);

        this._addEntryRow(convGroup, 'Personality', 'llm_system_prompt',
            'You are a helpful voice assistant. Keep responses concise and conversational.',
            'Instructions that shape how the AI talks to you');

        this._addSpinRow(convGroup, 'Reply Pause', 'conversation_silence_timeout',
            0.5, 10.0, 0.5, 1, 4.0,
            'Seconds of silence before your turn ends in a conversation. Default: 4');

        // ── Interrupting (barge-in, unified here) ──
        const bargeGroup = new Adw.PreferencesGroup({title: 'Interrupting'});
        page.add(bargeGroup);

        this._addSwitchRow(bargeGroup, 'Interrupt by Speaking',
            'Talking over the AI pauses its speech so it can hear you. Default: off',
            'enable_barge_in', false);

        this._addSpinRow(bargeGroup, 'Interrupt Threshold', 'barge_in_frames',
            1, 20, 1, 0, 3,
            'How much speech before it counts as an interruption — higher ignores brief noise. Default: 3');

        this._addSpinRow(bargeGroup, 'Resume After', 'barge_in_silence',
            0.3, 10.0, 0.1, 1, 1.0,
            'Seconds of silence before the AI resumes speaking. Default: 1');

        this._addSwitchRow(bargeGroup, 'Interrupt Chime',
            'Play a chime when an interruption is detected. Default: on',
            'chime_barge_in', true);

        // ── Generation (cloud-chat-assistant config) ──
        const genGroup = new Adw.PreferencesGroup({title: 'Generation'});
        page.add(genGroup);

        this._addCcaSpinRow(genGroup, 'Temperature', 'temperature',
            0.0, 2.0, 0.1, 1, 1.0,
            '0 = predictable, 2 = wild. Default: 1');

        this._addCcaSpinRow(genGroup, 'Reply Length Limit', 'max_completion_tokens',
            64, 128000, 256, 0, 2048,
            'Maximum tokens per reply — long limits cost more on cloud providers. Default: 2048');

        this._addCcaComboRow(genGroup, 'Reasoning Effort', 'reasoning_effort', [
            ['low', 'Low'],
            ['medium', 'Medium'],
            ['high', 'High'],
        ], 'high');

        this._addCcaSpinRow(genGroup, 'Memory Length', 'conversation_max_turns',
            1, 500, 10, 0, 50,
            'Conversation turns remembered before older ones are trimmed. Default: 50');

        this._addCcaSpinRow(genGroup, 'Multi-Chat Timeout', 'multi_chat_timeout',
            5, 120, 5, 0, 15,
            'Per-model timeout when comparing multiple models. Default: 15');

        // ── Notifications ──
        const notifGroup = new Adw.PreferencesGroup({title: 'Notifications'});
        page.add(notifGroup);

        this._addSwitchRow(notifGroup, 'Read Notifications Aloud',
            'Speak desktop notification titles and text as they arrive. "cast read notifications" toggles by voice. Default: off',
            'read_notifications', false);

        window.add(page);
    }

    // ═══════════════════════════════════════════════════════════════════
    // Page 3 — Voice & Sound: how it sounds and looks when it talks
    // ═══════════════════════════════════════════════════════════════════
    _addVoiceSoundPage(window) {
        const page = new Adw.PreferencesPage({
            title: 'Voice & Sound',
            icon_name: 'audio-speakers-symbolic',
        });

        // ── Voices ──
        const voiceGroup = new Adw.PreferencesGroup({title: 'Voices'});
        page.add(voiceGroup);

        const hdRow = this._addComboRow(voiceGroup, 'HD Voice', 'voice',
            HD_VOICES, 'en-US-Ava:DragonHDLatestNeural');
        hdRow.subtitle = 'Natural prosody, metered — costs per character. Needs an HD region (e.g. East US)';

        const fastRow = this._addComboRow(voiceGroup, 'Fast Voice', 'fast_voice',
            FAST_VOICES, 'en-US-AvaNeural');
        fastRow.subtitle = 'Low-latency neural voice (~120 ms), cheaper tier';

        this._addEntryRow(voiceGroup, 'Offline Voice', 'wyoming_tts_voice', 'en_GB-cori-high',
            'Piper voice used when the cloud is unreachable. Default: en_GB-cori-high');

        this._addSpinRow(voiceGroup, 'Speed', 'speed',
            0.5, 3.0, 0.1, 1, 1.0,
            'Speaking rate for cloud voices. Default: 1.0');

        this._addComboRow(voiceGroup, 'Pitch', 'pitch', [
            ['default', 'Default'],
            ['x-low', 'Extra Low'],
            ['low', 'Low'],
            ['medium', 'Medium'],
            ['high', 'High'],
            ['x-high', 'Extra High'],
        ], 'default');

        this._addComboRow(voiceGroup, 'Volume', 'volume', [
            ['default', 'Default'],
            ['silent', 'Silent'],
            ['x-soft', 'Extra Soft'],
            ['soft', 'Soft'],
            ['medium', 'Medium'],
            ['loud', 'Loud'],
            ['x-loud', 'Extra Loud'],
        ], 'default');

        // ── Subtitles — ONE switch, both layers ──
        const subGroup = new Adw.PreferencesGroup({title: 'Subtitles'});
        page.add(subGroup);

        this._addSubtitlesMasterRow(subGroup);

        this._addSwitchRow(subGroup, 'Your Voice',
            'Live transcript while you speak. Default: on',
            'subtitles_user', true);

        this._addSwitchRow(subGroup, 'Its Voice',
            'Captions while it speaks. Default: on',
            'subtitles_tts', true);

        this._addSwitchRow(subGroup, 'Word Highlights',
            'Newly heard words flash as they appear. Default: on',
            'show_word_highlights', true);

        this._addComboRow(subGroup, 'Your Words', 'subtitle_color_user',
            SUBTITLE_COLORS, 'light_green');

        this._addComboRow(subGroup, 'Its Words', 'subtitle_color_tts',
            SUBTITLE_COLORS, 'amber');

        // ── Chronicle ──
        const chronGroup = new Adw.PreferencesGroup({
            title: 'Chronicle',
            description: 'A local record of everything said — yours and ' +
                'its. Browse it from the panel menu and click any line to ' +
                'hear it again, or say "cast echo" to replay the last one.',
        });
        page.add(chronGroup);

        this._addSwitchRow(chronGroup, 'Keep the Chronicle',
            'Written to ~/.local/state/gnome-speaks/chronicle.jsonl — ' +
            'never leaves this machine. Default: on',
            'chronicle', true);

        // ── Chimes ──
        const chimeGroup = new Adw.PreferencesGroup({title: 'Chimes'});
        page.add(chimeGroup);

        this._addSwitchRow(chimeGroup, 'Ready',
            'Ascending tone when the microphone opens. Default: on',
            'chime_ready', true);

        this._addSwitchRow(chimeGroup, 'Recognized',
            'Blip when your speech is understood. Default: off',
            'chime_processing', false);

        this._addSwitchRow(chimeGroup, 'Speaking',
            'Descending tone before it starts talking. Default: off',
            'chime_speak', false);

        this._addSwitchRow(chimeGroup, 'Done',
            'Double-tap tone when it finishes talking. Default: off',
            'chime_done', false);

        this._addSwitchRow(chimeGroup, 'Thinking Hum',
            'Soft hum while the AI is thinking. Default: off',
            'chime_hum', false);

        // ── Badge Effects ──
        const fxGroup = new Adw.PreferencesGroup({title: 'Badge Effects'});
        page.add(fxGroup);

        this._addSwitchRow(fxGroup, 'Waveform',
            'Live audio bars under the badge while you speak. Default: on',
            'show_waveform', true);

        this._addSwitchRow(fxGroup, 'Speech Dot',
            'Green dot when your voice is detected. Default: on',
            'show_vad_dot', true);

        this._addSwitchRow(fxGroup, 'Silence Fade',
            'Waveform dims during long silence. Default: on',
            'show_silence_fade', true);

        this._addSwitchRow(fxGroup, 'Pulse',
            'Breathing animation while listening or speaking. Default: on',
            'show_badge_pulse', true);

        this._addSwitchRow(fxGroup, 'Volume Scale',
            'Badge grows with your voice volume. Default: on',
            'show_badge_scale', true);

        this._addSwitchRow(fxGroup, 'VU Meter',
            'Terminal volume meter during audio. Default: on',
            'vu_meter', true);

        this._addSwitchRow(fxGroup, 'Terminal Icons',
            'Status icons in terminal output. Default: on',
            'visual_indicator', true);

        // ── Desktop Voices (Spiel provider) ──
        const spielGroup = new Adw.PreferencesGroup({title: 'Desktop Voices (Spiel)'});
        page.add(spielGroup);

        this._addSwitchRow(spielGroup, 'Offer Voices to the Desktop',
            'Let screen readers and other apps use these voices (Spiel provider). Default: off',
            'spiel_provider', false);

        this._addListEntryRow(spielGroup, 'Offered Voices', 'spiel_voices',
            'en_GB-cori-high',
            'Comma-separated Piper voice names offered to apps. Default: en_GB-cori-high');

        this._addSwitchRow(spielGroup, 'Offer Cloud Voices Too',
            'Also offer the metered Azure voices — a screen reader narrating your desktop through these costs real money per sentence. Default: off',
            'spiel_expose_azure', false);

        window.add(page);
    }

    // ═══════════════════════════════════════════════════════════════════
    // Page 4 — Wake & Spells
    // ═══════════════════════════════════════════════════════════════════
    _addWakeSpellsPage(window) {
        const page = new Adw.PreferencesPage({
            title: 'Wake & Spells',
            icon_name: 'weather-clear-night-symbolic',
        });

        // ── Wake Word ──
        const wakeGroup = new Adw.PreferencesGroup({title: 'Wake Word'});
        page.add(wakeGroup);

        this._addSwitchRow(wakeGroup, 'Wake Word',
            'Say your wake phrase to open the microphone hands-free. Armed only while idle — never while dictating or speaking. "cast wake word" toggles by voice. Default: off',
            'wake_word', false);

        this._addEntryRow(wakeGroup, 'Wake Model', 'wake_word_model', '',
            'openwakeword model name — this IS your wake phrase; keep it private, since anyone who knows it can open your mic by voice');

        this._addSpinRow(wakeGroup, 'Wake Server Port', 'wyoming_wake_port',
            1, 65535, 1, 0, 10400,
            'openwakeword port on the offline server below. Default: 10400');

        // ── Voice Spellbook ──
        const spellGroup = new Adw.PreferencesGroup({
            title: 'Voice Spellbook',
            description: 'Say "cast …" or "invoke …" to run local spells instead of typing: mode switches, status reports, home rituals, oracle consultations. Test any spell without a mic: POST /cast on localhost:7710.',
        });
        page.add(spellGroup);

        const overlayRow = new Adw.ActionRow({
            title: 'Your Spells',
            subtitle: 'Edit ~/.config/speech-to-cli/spellbook.json — changes apply instantly',
        });
        overlayRow.add_suffix(new Gtk.Image({icon_name: 'document-edit-symbolic'}));
        overlayRow.activatable = true;
        overlayRow.connect('activated', () => {
            const path = GLib.get_home_dir() + '/.config/speech-to-cli/spellbook.json';
            Gio.AppInfo.launch_default_for_uri(`file://${path}`, null);
        });
        spellGroup.add(overlayRow);

        // ── Offline Server ──
        const offlineGroup = new Adw.PreferencesGroup({title: 'Offline Server'});
        page.add(offlineGroup);

        this._addEntryRow(offlineGroup, 'Server Address', 'wyoming_host', '',
            'LAN hostname or IP of your Wyoming server — speech falls back here when the cloud is unreachable. Empty = off');

        this._addSpinRow(offlineGroup, 'Voice Port', 'wyoming_tts_port',
            1, 65535, 1, 0, 10200,
            'Text-to-speech (e.g. wyoming-piper). Default: 10200');

        this._addSpinRow(offlineGroup, 'Hearing Port', 'wyoming_stt_port',
            1, 65535, 1, 0, 10300,
            'Speech-to-text (e.g. wyoming-onnx-asr). Default: 10300');

        window.add(page);
    }

    // ═══════════════════════════════════════════════════════════════════
    // Page 5 — Accounts: every credential in one place, costs visible
    // ═══════════════════════════════════════════════════════════════════
    _addAccountsPage(window) {
        const page = new Adw.PreferencesPage({
            title: 'Accounts',
            icon_name: 'dialog-password-symbolic',
        });

        // Metering banner — the one place costs are impossible to miss.
        const bannerGroup = new Adw.PreferencesGroup({});
        page.add(bannerGroup);
        const banner = new Adw.Banner({
            title: 'Cloud speech and AI are metered — these accounts bill per use. Local and offline features cost nothing.',
            revealed: true,
        });
        bannerGroup.add(banner);

        // ── Azure Speech ──
        const azureGroup = new Adw.PreferencesGroup({title: 'Azure Speech'});
        page.add(azureGroup);

        this._addPasswordRow(azureGroup, 'API Key (Azure Speech)', 'key',
            'Subscription key from portal.azure.com — used for cloud dictation and voices');

        this._addRegionCombo(azureGroup, 'Region', 'region',
            'Primary region for speech recognition and voices. Default: West US 2', 'westus2');

        this._addRegionCombo(azureGroup, 'HD Voice Region', 'tts_region',
            'Optional separate region for DragonHD voices (e.g. East US)', '');

        this._addPasswordRow(azureGroup, 'HD Voice Key', 'tts_key',
            'Optional separate key for the HD voice region — empty uses the primary key');

        // ── Azure AI Foundry ──
        const aiGroup = new Adw.PreferencesGroup({title: 'Azure AI Foundry'});
        page.add(aiGroup);

        this._addCcaEntryRow(aiGroup, 'Endpoint', 'endpoint', '',
            'Azure AI services endpoint URL');

        this._addCcaPasswordRow(aiGroup, 'API Key (Azure AI)', 'api_key',
            'For GPT, Grok, DeepSeek, Llama and other Azure AI models');

        this._addCcaComboRow(aiGroup, 'Model Type', 'model_type', [
            ['bedrock', 'AWS Bedrock'],
            ['deployed', 'Azure Deployed'],
            ['serverless', 'Azure Serverless'],
            ['google', 'Google Vertex AI'],
        ], 'bedrock');

        this._addCcaEntryRow(aiGroup, 'Deployment', 'deployment', '',
            'Azure deployment name, for deployed models only');

        // ── AWS Bedrock ──
        const awsGroup = new Adw.PreferencesGroup({title: 'AWS Bedrock'});
        page.add(awsGroup);

        this._addCcaPasswordRow(awsGroup, 'Access Key (AWS)', 'aws_access_key',
            'AWS Access Key ID — Bedrock bills as AWS Marketplace, promotional credits do not apply');

        this._addCcaPasswordRow(awsGroup, 'Secret Key (AWS)', 'aws_secret_key',
            'AWS Secret Access Key');

        this._addCcaComboRow(awsGroup, 'Region', 'aws_region', [
            ['us-east-1', 'US East 1 (N. Virginia)'],
            ['us-west-2', 'US West 2 (Oregon)'],
            ['eu-west-1', 'EU West 1 (Ireland)'],
            ['eu-central-1', 'EU Central 1 (Frankfurt)'],
            ['ap-southeast-1', 'AP Southeast 1 (Singapore)'],
            ['ap-northeast-1', 'AP Northeast 1 (Tokyo)'],
        ], 'us-east-1');

        // ── Google Vertex AI ──
        const googleGroup = new Adw.PreferencesGroup({title: 'Google Vertex AI'});
        page.add(googleGroup);

        this._addCcaPasswordRow(googleGroup, 'API Key (Google)', 'google_api_key',
            'Google Cloud API key for Gemini models');

        this._addCcaEntryRow(googleGroup, 'Project ID', 'google_project', '',
            'GCP project ID or number');

        this._addCcaComboRow(googleGroup, 'Region', 'google_region', [
            ['global', 'Global'],
            ['us-east4', 'US East 4'],
            ['us-central1', 'US Central 1'],
            ['us-west1', 'US West 1'],
            ['europe-west1', 'Europe West 1'],
            ['europe-west4', 'Europe West 4'],
            ['asia-southeast1', 'Asia Southeast 1'],
        ], 'global');

        window.add(page);
    }

    // ═══════════════════════════════════════════════════════════════════
    // Page 6 — Advanced
    // ═══════════════════════════════════════════════════════════════════
    _addAdvancedPage(window) {
        const page = new Adw.PreferencesPage({
            title: 'Advanced',
            icon_name: 'preferences-other-symbolic',
        });

        // ── Audio Devices ──
        const sinks = this._enumeratePipeWireDevices('sinks');
        const sources = this._enumeratePipeWireDevices('sources');

        const devGroup = new Adw.PreferencesGroup({title: 'Audio Devices'});
        page.add(devGroup);

        const playerOptions = [['auto', 'Auto-detect']];
        for (const [cmd, label] of [
            ['aplay', 'aplay (ALSA)'],
            ['pw-play', 'pw-play (PipeWire)'],
            ['pw-cat', 'pw-cat (PipeWire)'],
            ['ffplay', 'ffplay (FFmpeg)'],
        ]) {
            playerOptions.push([cmd,
                this._commandExists(cmd) ? label : `${label} — not found`]);
        }
        this._addComboRow(devGroup, 'Player', 'player', playerOptions, 'auto');

        this._addComboRow(devGroup, 'Speaker', 'speaker_sink',
            [['', 'System Default'], ...sinks], '');

        const recorderOptions = [['auto', 'Auto-detect']];
        for (const [cmd, label] of [
            ['pw-record', 'pw-record (PipeWire)'],
            ['arecord', 'arecord (ALSA)'],
        ]) {
            recorderOptions.push([cmd,
                this._commandExists(cmd) ? label : `${label} — not found`]);
        }
        this._addComboRow(devGroup, 'Recorder', 'recorder', recorderOptions, 'auto');

        this._addComboRow(devGroup, 'Microphone', 'mic_source',
            [['', 'System Default'], ...sources], '');

        const duplexRow = this._addComboRow(devGroup, 'Speak While Listening', 'half_duplex', [
            ['auto', 'Auto — speakers take turns, headphones overlap'],
            ['true', 'Take Turns (speak, then listen)'],
            ['false', 'Overlap (speak and listen together)'],
        ], 'auto');
        duplexRow.subtitle = 'Whether talking and listening can happen at the same time. Default: Auto';

        this._addSwitchRow(devGroup, 'Echo Cancellation',
            'Use PipeWire echo-cancel nodes when available, so it does not hear itself. Default: off',
            'enable_echo_cancel', false);

        this._addSwitchRow(devGroup, 'Allow Pause',
            'Allow pausing and resuming playback. Default: on',
            'enable_pause', true);

        this._addSpinRow(devGroup, 'Talk Mode Pause', 'talk_silence_timeout',
            0.5, 10.0, 0.5, 1, 4.0,
            'Silence timeout for external apps using the Talk D-Bus call. Default: 4');

        // ── Corrections ──
        const correctGroup = new Adw.PreferencesGroup({title: 'Auto-Corrections'});
        page.add(correctGroup);

        this._addCorrectionsRow(correctGroup);

        // ── Privacy & Debug ──
        const privGroup = new Adw.PreferencesGroup({title: 'Privacy & Debug'});
        page.add(privGroup);

        this._addEntryRow(privGroup, 'Save Spoken Audio To', 'save_audio_dir', '',
            'Folder where every TTS utterance is saved as audio — fills up and records everything said. Empty = off');

        this._addSwitchRow(privGroup, 'Debug Log',
            'Write detailed logs to /tmp/speech-debug.log — includes everything you dictate. Default: off',
            'debug', false);

        // ── Service ──
        const serviceGroup = new Adw.PreferencesGroup({title: 'Service'});
        page.add(serviceGroup);

        const restartRow = new Adw.ActionRow({
            title: 'Restart Speech Service',
            subtitle: 'Most settings apply live — restart after changing devices, accounts, or the AI provider',
        });
        const restartButton = new Gtk.Button({
            label: 'Restart',
            valign: Gtk.Align.CENTER,
            css_classes: ['suggested-action'],
        });
        restartButton.connect('clicked', () => this._restartService(restartButton));
        restartRow.add_suffix(restartButton);
        restartRow.set_activatable_widget(restartButton);
        serviceGroup.add(restartRow);

        window.add(page);
    }

    // ═══════════════════════════════════════════════════════════════════
    // Special rows
    // ═══════════════════════════════════════════════════════════════════

    /**
     * The Live Subtitles master switch. One concept lives in two stores:
     * GSettings `live-subtitles` gates the extension's overlay rendering,
     * config `live_subtitles` gates the service's subtitle stream (and the
     * "cast subtitles" spell flips the config side). This row writes BOTH,
     * and follows external changes to either.
     */
    _addSubtitlesMasterRow(group) {
        const row = new Adw.SwitchRow({
            title: 'Live Subtitles',
            subtitle: 'Show the words as they are heard and spoken. "cast subtitles" toggles by voice. Default: on',
            active: this._settings.get_boolean('live-subtitles'),
        });

        row.connect('notify::active', () => {
            if (this._settings.get_boolean('live-subtitles') !== row.active)
                this._settings.set_boolean('live-subtitles', row.active);
            this._setConfigValue('live_subtitles', row.active);
        });

        // Follow the GSettings side if something else (or the spell path,
        // which syncs GSettings too) changes it while the window is open.
        const changedId = this._settings.connect('changed::live-subtitles', () => {
            const val = this._settings.get_boolean('live-subtitles');
            if (row.active !== val)
                row.active = val;
        });
        row.connect('destroy', () => this._settings.disconnect(changedId));

        group.add(row);
        return row;
    }

    /** Entry row whose config value is a LIST, edited as comma-separated. */
    _addListEntryRow(group, title, configKey, defaultValue, help) {
        const current = this._config[configKey];
        const text = Array.isArray(current) ? current.join(', ')
            : (current || defaultValue);
        const row = new Adw.EntryRow({
            title: title,
            text: text,
            show_apply_button: true,
            tooltip_text: help || '',
        });
        row.connect('apply', () => {
            const items = row.get_text().split(',')
                .map(s => s.trim()).filter(s => s.length);
            if (items.length === 0)
                this._deleteConfigKey(configKey);
            else
                this._setConfigValue(configKey, items);
        });
        group.add(row);
        return row;
    }

    // ═══════════════════════════════════════════════════════════════════
    // Widget Helpers — config.json backed
    // ═══════════════════════════════════════════════════════════════════

    _addComboRow(group, title, configKey, options, defaultValue) {
        const values = options.map(o => o[0]);
        const labels = options.map(o => o[1]);

        const currentValue = this._config[configKey] ?? defaultValue;
        let selectedIdx = values.findIndex(v => String(v) === String(currentValue));
        if (selectedIdx < 0) selectedIdx = 0;

        const model = Gtk.StringList.new(labels);
        const row = new Adw.ComboRow({
            title: title,
            model: model,
            selected: selectedIdx,
        });

        row.connect('notify::selected', () => {
            const idx = row.get_selected();
            if (idx >= 0 && idx < values.length) {
                const val = values[idx];
                if (val === '' || val === null)
                    this._deleteConfigKey(configKey);
                else
                    this._setConfigValue(configKey, val);
            }
        });

        group.add(row);
        return row;
    }

    _addRegionCombo(group, title, configKey, subtitle, defaultValue) {
        const regions = [...REGIONS];
        const currentValue = this._config[configKey] || defaultValue;

        if (currentValue && !regions.find(r => r[0] === currentValue))
            regions.push([currentValue, `${currentValue} (custom)`]);

        if (defaultValue === '')
            regions.unshift(['', 'Same as primary region']);

        const values = regions.map(r => r[0]);
        const labels = regions.map(r => r[0] ? `${r[1]} (${r[0]})` : r[1]);

        let selectedIdx = values.indexOf(currentValue);
        if (selectedIdx < 0) selectedIdx = 0;

        const model = Gtk.StringList.new(labels);
        const row = new Adw.ComboRow({
            title: title,
            subtitle: subtitle || '',
            model: model,
            selected: selectedIdx,
        });

        row.connect('notify::selected', () => {
            const idx = row.get_selected();
            if (idx >= 0 && idx < values.length) {
                const val = values[idx];
                if (val === '')
                    this._deleteConfigKey(configKey);
                else
                    this._setConfigValue(configKey, val);
            }
        });

        group.add(row);
        return row;
    }

    _addSwitchRow(group, title, subtitle, configKey, defaultValue) {
        const currentValue = this._config[configKey] ?? defaultValue;
        const row = new Adw.SwitchRow({
            title: title,
            subtitle: subtitle || '',
            active: !!currentValue,
        });

        row.connect('notify::active', () => {
            this._setConfigValue(configKey, row.active);
        });

        group.add(row);
        return row;
    }

    _addSpinRow(group, title, configKey, lower, upper, step, digits, defaultValue, subtitle) {
        const currentValue = this._config[configKey] ?? defaultValue;
        const adjustment = new Gtk.Adjustment({
            lower: lower,
            upper: upper,
            step_increment: step,
            page_increment: step * 10,
            value: currentValue,
        });

        const row = new Adw.SpinRow({
            title: title,
            subtitle: subtitle || '',
            adjustment: adjustment,
            digits: digits,
            value: currentValue,
        });

        row.connect('notify::value', () => {
            const val = digits > 0
                ? Math.round(row.value * Math.pow(10, digits)) / Math.pow(10, digits)
                : Math.round(row.value);
            this._setConfigValue(configKey, val);
        });

        group.add(row);
        return row;
    }

    /**
     * Adw.EntryRow has no subtitle property, which is how nine help texts
     * silently vanished in the previous layout — the help now renders as a
     * tooltip (hover/focus), and cost/privacy caveats are worded into row
     * TITLES or neighboring rows where they must be seen, not hovered.
     */
    _addEntryRow(group, title, configKey, defaultValue, help) {
        const currentValue = this._config[configKey] ?? defaultValue;
        const row = new Adw.EntryRow({
            title: title,
            text: currentValue != null ? String(currentValue) : '',
            show_apply_button: true,
            tooltip_text: help || '',
        });

        row.connect('apply', () => {
            const text = row.get_text().trim();
            if (text === '')
                this._deleteConfigKey(configKey);
            else
                this._setConfigValue(configKey, text);
        });

        group.add(row);
        return row;
    }

    _addPasswordRow(group, title, configKey, help) {
        const currentValue = this._config[configKey] || '';

        let row;
        try {
            row = new Adw.PasswordEntryRow({
                title: title,
                text: currentValue,
                show_apply_button: true,
                tooltip_text: help || '',
            });
        } catch (e) {
            row = new Adw.EntryRow({
                title: title,
                text: currentValue,
                show_apply_button: true,
                tooltip_text: help || '',
            });
        }

        row.connect('apply', () => {
            const text = row.get_text().trim();
            if (text === '')
                this._deleteConfigKey(configKey);
            else
                this._setConfigValue(configKey, text);
        });

        group.add(row);
        return row;
    }

    _addCorrectionsRow(group) {
        // Read from this._config on every open, not once at page-build time:
        // _setConfigValue() mutates this._config in place, so a captured
        // snapshot would re-show pre-save text the second time you edit.
        const asText = () => Object.entries(this._config['auto_corrections'] || {})
            .map(([wrong, right]) => `${wrong}=${right}`)
            .join('\n');
        const summary = n => `${n} defined — fix words it always mishears (wrong=right)`;

        const row = new Adw.ActionRow({
            title: 'Word Corrections',
            subtitle: summary(Object.keys(this._config['auto_corrections'] || {}).length),
        });

        const editButton = new Gtk.Button({
            label: 'Edit',
            valign: Gtk.Align.CENTER,
        });

        editButton.connect('clicked', () => {
            // Adw.AlertDialog, not Gtk.Dialog: Gtk.Dialog is deprecated since
            // GTK 4.10, and the old call also presented without a parent, so
            // the "modal" editor floated free of the prefs window. Mirrors the
            // shortcut editor below.
            const dialog = new Adw.AlertDialog({
                heading: 'Auto-Corrections',
                body: 'One correction per line: wrong=right',
            });

            const textView = new Gtk.TextView({
                editable: true,
                wrap_mode: Gtk.WrapMode.WORD,
                monospace: true,
                top_margin: 8,
                bottom_margin: 8,
                left_margin: 8,
                right_margin: 8,
            });
            textView.buffer.set_text(asText(), -1);

            dialog.set_extra_child(new Gtk.ScrolledWindow({
                child: textView,
                vexpand: true,
                hexpand: true,
                width_request: 380,
                height_request: 260,
            }));

            dialog.add_response('cancel', 'Cancel');
            dialog.add_response('save', 'Save');
            dialog.set_response_appearance('save', Adw.ResponseAppearance.SUGGESTED);
            dialog.set_default_response('save');
            dialog.set_close_response('cancel');

            dialog.connect('response', (dlg, response) => {
                if (response !== 'save')
                    return;
                let [start, end] = textView.buffer.get_bounds();
                let newText = textView.buffer.get_text(start, end, false);
                let newCorrections = {};
                for (let line of newText.split('\n')) {
                    line = line.trim();
                    if (!line || !line.includes('=')) continue;
                    let [wrong, ...rightParts] = line.split('=');
                    newCorrections[wrong.trim()] = rightParts.join('=').trim();
                }
                this._setConfigValue('auto_corrections', newCorrections);
                row.subtitle = summary(Object.keys(newCorrections).length);
            });

            dialog.present(group.get_root());
        });

        row.add_suffix(editButton);
        row.set_activatable_widget(editButton);
        group.add(row);
    }

    _addShortcutRow(group, title, settingsKey) {
        const shortcuts = this._settings.get_strv(settingsKey);
        const currentShortcut = shortcuts.length > 0 ? shortcuts[0] : 'Disabled';

        const row = new Adw.ActionRow({
            title: title,
            subtitle: currentShortcut,
        });

        const editButton = new Gtk.Button({
            label: 'Change',
            valign: Gtk.Align.CENTER,
        });

        editButton.connect('clicked', () => {
            const dialog = new Adw.AlertDialog({
                heading: `Set shortcut for "${title}"`,
                body: 'Enter a keyboard shortcut (e.g. <Super><Alt>space):',
            });

            const entry = new Gtk.Entry({
                text: currentShortcut,
                margin_start: 16,
                margin_end: 16,
                margin_bottom: 8,
            });
            dialog.set_extra_child(entry);

            dialog.add_response('cancel', 'Cancel');
            dialog.add_response('disable', 'Disable');
            dialog.add_response('save', 'Save');
            dialog.set_response_appearance('save', Adw.ResponseAppearance.SUGGESTED);
            dialog.set_default_response('save');
            dialog.set_close_response('cancel');

            dialog.connect('response', (dlg, response) => {
                if (response === 'save') {
                    let val = entry.get_text().trim();
                    if (val) {
                        this._settings.set_strv(settingsKey, [val]);
                        row.subtitle = val;
                    }
                } else if (response === 'disable') {
                    this._settings.set_strv(settingsKey, []);
                    row.subtitle = 'Disabled';
                }
            });

            dialog.present(group.get_root());
        });

        row.add_suffix(editButton);
        row.set_activatable_widget(editButton);
        group.add(row);
    }

    // ═══════════════════════════════════════════════════════════════════
    // Widget Helpers — GSettings backed (extension settings)
    // ═══════════════════════════════════════════════════════════════════

    _addGSettingsSwitchRow(group, title, subtitle, settingsKey) {
        const row = new Adw.SwitchRow({
            title: title,
            subtitle: subtitle || '',
            active: this._settings.get_boolean(settingsKey),
        });

        this._settings.bind(settingsKey, row, 'active',
            Gio.SettingsBindFlags.DEFAULT);

        group.add(row);
        return row;
    }

    _addGSettingsSpinRow(group, title, settingsKey, lower, upper, step, digits, subtitle) {
        const adjustment = new Gtk.Adjustment({
            lower: lower,
            upper: upper,
            step_increment: step,
            page_increment: step * 10,
            value: this._settings.get_int(settingsKey),
        });

        const row = new Adw.SpinRow({
            title: title,
            subtitle: subtitle || '',
            adjustment: adjustment,
            digits: digits,
            value: this._settings.get_int(settingsKey),
        });

        row.connect('notify::value', () => {
            this._settings.set_int(settingsKey, Math.round(row.value));
        });

        group.add(row);
        return row;
    }

    // ═══════════════════════════════════════════════════════════════════
    // System probes
    // ═══════════════════════════════════════════════════════════════════

    /**
     * Enumerate PipeWire sinks or sources by parsing `wpctl status`.
     * Returns [[nodeId, label], ...] suitable for _addComboRow.
     */
    _enumeratePipeWireDevices(type) {
        const devices = [];
        try {
            const [ok, stdout, stderr, exitCode] = GLib.spawn_command_line_sync('wpctl status');
            if (!ok || exitCode !== 0) return devices;

            const output = new TextDecoder('utf-8').decode(stdout);
            const lines = output.split('\n');

            const header = type === 'sinks' ? 'Sinks:' : 'Sources:';
            let inAudio = false;
            let inSection = false;

            for (const line of lines) {
                const trimmed = line.trim();

                if (trimmed === 'Audio') {
                    inAudio = true;
                    continue;
                }
                if (inAudio && /^(Video|Settings)$/.test(trimmed)) {
                    break;
                }
                if (!inAudio) continue;

                if (trimmed.endsWith(header)) {
                    inSection = true;
                    continue;
                }

                if (inSection && (trimmed === '│' || trimmed === '' ||
                    (trimmed.endsWith(':') && !trimmed.match(/^\d/)))) {
                    break;
                }

                if (!inSection) continue;

                const stripped = trimmed.replace(/^[│├└─┬┤┼╌╎\s]+/, '');
                const match = stripped.match(/^(\*?)\s*(\d+)\.\s+(.+?)(?:\s+\[.*\])?\s*$/);
                if (match) {
                    const isDefault = match[1] === '*';
                    const nodeId = match[2];
                    const name = match[3].trim();
                    const label = isDefault ? `${name} (default)` : name;
                    devices.push([nodeId, label]);
                }
            }
        } catch (e) {
            // wpctl not available — return empty list
        }
        return devices;
    }

    _commandExists(cmd) {
        try {
            const [ok, stdout, stderr, exitCode] = GLib.spawn_command_line_sync(`which ${cmd}`);
            return ok && exitCode === 0;
        } catch (e) {
            return false;
        }
    }

    // ═══════════════════════════════════════════════════════════════════
    // Config I/O — ~/.config/speech-to-cli/config.json
    // ═══════════════════════════════════════════════════════════════════

    _loadConfig() {
        const path = GLib.build_filenamev([
            GLib.get_home_dir(), '.config', 'speech-to-cli', 'config.json',
        ]);
        try {
            const [ok, contents] = GLib.file_get_contents(path);
            if (ok) {
                const decoder = new TextDecoder('utf-8');
                return JSON.parse(decoder.decode(contents));
            }
        } catch (e) {
            // File doesn't exist or parse error — start fresh
        }
        return {};
    }

    _saveConfig() {
        const dir = GLib.build_filenamev([
            GLib.get_home_dir(), '.config', 'speech-to-cli',
        ]);
        GLib.mkdir_with_parents(dir, 0o755);
        const path = GLib.build_filenamev([dir, 'config.json']);

        // Merge-on-write (#16): re-read the file and apply only OUR edits
        // on top — the service (spells, quality toggle) writes this file
        // too, and dumping a stale full object would erase its changes.
        let onDisk = {};
        try {
            const [ok, contents] = GLib.file_get_contents(path);
            if (ok)
                onDisk = JSON.parse(new TextDecoder('utf-8').decode(contents));
        } catch (e) {
            // Missing/corrupt file — our object is the best we have
            onDisk = {};
        }
        for (const key of this._dirtyKeys ?? [])
            onDisk[key] = this._config[key];
        for (const key of this._deletedKeys ?? [])
            delete onDisk[key];
        this._config = onDisk;
        this._dirtyKeys = new Set();
        this._deletedKeys = new Set();

        const json = JSON.stringify(onDisk, null, 2) + '\n';
        const encoder = new TextEncoder();
        GLib.file_set_contents(path, encoder.encode(json));
    }

    _setConfigValue(key, value) {
        this._config[key] = value;
        (this._dirtyKeys ??= new Set()).add(key);
        (this._deletedKeys ??= new Set()).delete(key);
        this._scheduleConfigSave();
    }

    _deleteConfigKey(key) {
        delete this._config[key];
        (this._deletedKeys ??= new Set()).add(key);
        (this._dirtyKeys ??= new Set()).delete(key);
        this._scheduleConfigSave();
    }

    _scheduleConfigSave() {
        if (this._saveTimeoutId) {
            GLib.Source.remove(this._saveTimeoutId);
            this._saveTimeoutId = null;
        }
        this._saveTimeoutId = GLib.timeout_add(GLib.PRIORITY_DEFAULT, 500, () => {
            this._saveConfig();
            this._saveTimeoutId = null;
            return GLib.SOURCE_REMOVE;
        });
    }

    _flushConfigSave() {
        if (this._saveTimeoutId) {
            GLib.Source.remove(this._saveTimeoutId);
            this._saveTimeoutId = null;
            this._saveConfig();
        }
    }

    // ═══════════════════════════════════════════════════════════════════
    // Config I/O — ~/.config/cloud-chat-assistant/config.json
    // ═══════════════════════════════════════════════════════════════════

    _loadCcaConfig() {
        const path = GLib.build_filenamev([
            GLib.get_home_dir(), '.config', 'cloud-chat-assistant', 'config.json',
        ]);
        try {
            const [ok, contents] = GLib.file_get_contents(path);
            if (ok) {
                const decoder = new TextDecoder('utf-8');
                return JSON.parse(decoder.decode(contents));
            }
        } catch (e) {
            // File doesn't exist or parse error — start fresh
        }
        return {};
    }

    _saveCcaConfig() {
        const dir = GLib.build_filenamev([
            GLib.get_home_dir(), '.config', 'cloud-chat-assistant',
        ]);
        GLib.mkdir_with_parents(dir, 0o755);
        const path = GLib.build_filenamev([dir, 'config.json']);

        // Merge-on-write, same rationale as _saveConfig (#16).
        let onDisk = {};
        try {
            const [ok, contents] = GLib.file_get_contents(path);
            if (ok)
                onDisk = JSON.parse(new TextDecoder('utf-8').decode(contents));
        } catch (e) {
            onDisk = {};
        }
        for (const key of this._ccaDirtyKeys ?? [])
            onDisk[key] = this._ccaConfig[key];
        for (const key of this._ccaDeletedKeys ?? [])
            delete onDisk[key];
        this._ccaConfig = onDisk;
        this._ccaDirtyKeys = new Set();
        this._ccaDeletedKeys = new Set();

        const json = JSON.stringify(onDisk, null, 2) + '\n';
        const encoder = new TextEncoder();
        GLib.file_set_contents(path, encoder.encode(json));
    }

    _setCcaConfigValue(key, value) {
        this._ccaConfig[key] = value;
        (this._ccaDirtyKeys ??= new Set()).add(key);
        (this._ccaDeletedKeys ??= new Set()).delete(key);
        this._scheduleCcaConfigSave();
    }

    _deleteCcaConfigKey(key) {
        delete this._ccaConfig[key];
        (this._ccaDeletedKeys ??= new Set()).add(key);
        (this._ccaDirtyKeys ??= new Set()).delete(key);
        this._scheduleCcaConfigSave();
    }

    _scheduleCcaConfigSave() {
        if (this._ccaSaveTimeoutId) {
            GLib.Source.remove(this._ccaSaveTimeoutId);
            this._ccaSaveTimeoutId = null;
        }
        this._ccaSaveTimeoutId = GLib.timeout_add(GLib.PRIORITY_DEFAULT, 500, () => {
            this._saveCcaConfig();
            this._ccaSaveTimeoutId = null;
            return GLib.SOURCE_REMOVE;
        });
    }

    _flushCcaConfigSave() {
        if (this._ccaSaveTimeoutId) {
            GLib.Source.remove(this._ccaSaveTimeoutId);
            this._ccaSaveTimeoutId = null;
            this._saveCcaConfig();
        }
    }

    // ═══════════════════════════════════════════════════════════════════
    // Widget Helpers — cloud-chat-assistant config backed
    // ═══════════════════════════════════════════════════════════════════

    _addCcaComboRow(group, title, configKey, options, defaultValue) {
        const values = options.map(o => o[0]);
        const labels = options.map(o => o[1]);

        const currentValue = this._ccaConfig[configKey] ?? defaultValue;
        let selectedIdx = values.findIndex(v => String(v) === String(currentValue));
        if (selectedIdx < 0) selectedIdx = 0;

        const model = Gtk.StringList.new(labels);
        const row = new Adw.ComboRow({
            title: title,
            model: model,
            selected: selectedIdx,
        });

        row.connect('notify::selected', () => {
            const idx = row.get_selected();
            if (idx >= 0 && idx < values.length) {
                const val = values[idx];
                if (val === '' || val === null)
                    this._deleteCcaConfigKey(configKey);
                else
                    this._setCcaConfigValue(configKey, val);
            }
        });

        group.add(row);
        return row;
    }

    _addCcaEntryRow(group, title, configKey, defaultValue, help) {
        const currentValue = this._ccaConfig[configKey] ?? defaultValue;
        const row = new Adw.EntryRow({
            title: title,
            text: currentValue != null ? String(currentValue) : '',
            show_apply_button: true,
            tooltip_text: help || '',
        });

        row.connect('apply', () => {
            const text = row.get_text().trim();
            if (text === '')
                this._deleteCcaConfigKey(configKey);
            else
                this._setCcaConfigValue(configKey, text);
        });

        group.add(row);
        return row;
    }

    _addCcaPasswordRow(group, title, configKey, help) {
        const currentValue = this._ccaConfig[configKey] || '';

        let row;
        try {
            row = new Adw.PasswordEntryRow({
                title: title,
                text: currentValue,
                show_apply_button: true,
                tooltip_text: help || '',
            });
        } catch (e) {
            row = new Adw.EntryRow({
                title: title,
                text: currentValue,
                show_apply_button: true,
                tooltip_text: help || '',
            });
        }

        row.connect('apply', () => {
            const text = row.get_text().trim();
            if (text === '')
                this._deleteCcaConfigKey(configKey);
            else
                this._setCcaConfigValue(configKey, text);
        });

        group.add(row);
        return row;
    }

    _addCcaSpinRow(group, title, configKey, lower, upper, step, digits, defaultValue, subtitle) {
        const currentValue = this._ccaConfig[configKey] ?? defaultValue;
        const adjustment = new Gtk.Adjustment({
            lower: lower,
            upper: upper,
            step_increment: step,
            page_increment: step * 10,
            value: currentValue,
        });

        const row = new Adw.SpinRow({
            title: title,
            subtitle: subtitle || '',
            adjustment: adjustment,
            digits: digits,
            value: currentValue,
        });

        row.connect('notify::value', () => {
            const val = digits > 0
                ? Math.round(row.value * Math.pow(10, digits)) / Math.pow(10, digits)
                : Math.round(row.value);
            this._setCcaConfigValue(configKey, val);
        });

        group.add(row);
        return row;
    }

    // ═══════════════════════════════════════════════════════════════════
    // Service Management
    // ═══════════════════════════════════════════════════════════════════

    _restartService(button) {
        button.set_sensitive(false);
        button.set_label('Restarting…');

        try {
            GLib.spawn_command_line_async(
                'systemctl --user restart gnome-speaks.service'
            );
            GLib.timeout_add(GLib.PRIORITY_DEFAULT, 2000, () => {
                button.set_label('Restart');
                button.set_sensitive(true);
                return GLib.SOURCE_REMOVE;
            });
        } catch (e) {
            button.set_label('Failed');
            GLib.timeout_add(GLib.PRIORITY_DEFAULT, 2000, () => {
                button.set_label('Restart');
                button.set_sensitive(true);
                return GLib.SOURCE_REMOVE;
            });
        }
    }
}
