// SPDX-License-Identifier: GPL-3.0-or-later
// GNOME Speaks — TTS/STT floating badge for GNOME Shell
// Copyright (C) 2025 JP Hein
//
// This program is free software: you can redistribute it and/or modify
// it under the terms of the GNU General Public License as published by
// the Free Software Foundation, either version 3 of the License, or
// (at your option) any later version.

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

        this._addModesPage(window);
        this._addAzurePage(window);
        this._addCloudAIPage(window);
        this._addVoicePage(window);
        this._addListeningPage(window);
        this._addAudioPage(window);
        this._addSpellcraftPage(window);
        this._addFeedbackPage(window);
        this._addExtensionPage(window);
        this._addAdvancedPage(window);
    }

    // ═══════════════════════════════════════════════════════════════════
    // Page 0 — Modes Guide
    // ═══════════════════════════════════════════════════════════════════
    _addModesPage(window) {
        const page = new Adw.PreferencesPage({
            title: 'Modes',
            icon_name: 'view-list-symbolic',
        });

        // ── Type Mode ──
        const typeGroup = new Adw.PreferencesGroup({
            title: 'Type Mode',
            description: 'Speech is transcribed and typed at the cursor via wtype (Wayland) or xdotool (X11). Click the floating badge or press Super+Alt+Space to start. Click again, say "over", or wait for silence to stop.',
        });
        page.add(typeGroup);

        this._addSwitchRow(typeGroup, 'Type at Cursor',
            'Type transcribed text where the cursor is. When off, text is copied to clipboard only.',
            'dictation_mode', true);

        this._addSwitchRow(typeGroup, 'Skip Final Paste',
            'Keep live-typed text as-is instead of replacing it with the final corrected transcription',
            'skip_final_paste', true);

        this._addSwitchRow(typeGroup, 'Terminal Mode',
            'All lowercase, no auto-capitalization or punctuation. Uses Azure Lexical output for code and terminal input.',
            'terminal_mode', false);

        this._addSwitchRow(typeGroup, 'Voice Commands',
            'Convert spoken punctuation ("period", "comma", "new line") to characters',
            'voice_commands', true);

        this._addEntryRow(typeGroup, 'End Word', 'end_word', 'over',
            'Say this word to immediately stop recording');

        // ── AI Mode ──
        const aiGroup = new Adw.PreferencesGroup({
            title: 'AI Mode',
            description: 'Speech is sent to an LLM (Claude, GPT, etc.) and the response is spoken aloud. Toggle with the robot pill on the badge, or the panel menu.',
        });
        page.add(aiGroup);

        this._addSwitchRow(aiGroup, 'AI Conversation',
            'Send transcriptions to an LLM and speak the response aloud',
            'conversation_mode', false);

        this._addComboRow(aiGroup, 'LLM Provider', 'llm_provider', [
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

        this._addComboRow(aiGroup, 'LLM Model', 'llm_model', [
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

        this._addPasswordRow(aiGroup, 'LLM API Key', 'llm_api_key',
            'API key for the selected LLM provider');

        this._addEntryRow(aiGroup, 'System Prompt', 'llm_system_prompt',
            'You are a helpful voice assistant. Keep responses concise and conversational.',
            'Instructions for the LLM persona');

        // Restart Service button
        const restartRow = new Adw.ActionRow({
            title: 'Restart Service',
            subtitle: 'Apply provider/model changes immediately',
        });
        const restartBtn = new Gtk.Button({
            label: 'Restart',
            valign: Gtk.Align.CENTER,
            css_classes: ['suggested-action'],
        });
        restartBtn.connect('clicked', () => {
            try {
                GLib.spawn_command_line_async('systemctl --user restart gnome-speaks.service');
                restartBtn.label = 'Restarted';
                restartBtn.sensitive = false;
                GLib.timeout_add(GLib.PRIORITY_DEFAULT, 2000, () => {
                    restartBtn.label = 'Restart';
                    restartBtn.sensitive = true;
                    return GLib.SOURCE_REMOVE;
                });
            } catch (e) {
                log(`Failed to restart service: ${e.message}`);
            }
        });
        restartRow.add_suffix(restartBtn);
        aiGroup.add(restartRow);

        // ── Continuous Dictation ──
        const contGroup = new Adw.PreferencesGroup({
            title: 'Continuous Dictation',
            description: 'After each pause, listening automatically restarts so you can dictate without re-clicking the badge. Works in both Type and AI modes.',
        });
        page.add(contGroup);

        this._addSwitchRow(contGroup, 'Continuous Dictation',
            'Automatically restart listening after each utterance',
            'continuous_dictation', false);

        this._addSpinRow(contGroup, 'Silence Timeout', 'silence_timeout',
            0.5, 10.0, 0.5, 1, 3.0,
            'Seconds of silence after speech before auto-stop');

        this._addSpinRow(contGroup, 'No Speech Timeout', 'no_speech_timeout',
            1.0, 30.0, 1.0, 0, 7.0,
            'Max seconds to wait for any speech before giving up');

        this._addSpinRow(contGroup, 'Loop Silence Timeout', 'loop_silence_timeout',
            0.3, 5.0, 0.1, 1, 1.2,
            'Silence timeout in continuous loop mode (shorter = faster turnaround)');

        // ── Barge-in ──
        const bargeGroup = new Adw.PreferencesGroup({
            title: 'Barge-in',
            description: 'Interrupt TTS playback by speaking. The AI pauses, listens to you, then resumes or responds.',
        });
        page.add(bargeGroup);

        this._addSwitchRow(bargeGroup, 'Enable Barge-in',
            'Pause TTS when you start speaking',
            'enable_barge_in', false);

        // ── Notification Reader ──
        const notifGroup = new Adw.PreferencesGroup({
            title: 'Notification Reader',
            description: 'Automatically read GNOME desktop notifications aloud as they arrive.',
        });
        page.add(notifGroup);

        this._addSwitchRow(notifGroup, 'Read Notifications',
            'Speak notification titles and body text',
            'read_notifications', false);

        // ── Mode Combinations ──
        const comboGroup = new Adw.PreferencesGroup({
            title: 'Mode Combinations',
            description: 'Modes combine to create different workflows. Here is what happens with each combination of toggles.',
        });
        page.add(comboGroup);

        this._addInfoRow(comboGroup, 'Type only',
            'Click badge, speak, text typed at cursor, done. The simplest mode.');
        this._addInfoRow(comboGroup, 'Type + Continuous',
            'Click badge, speak, text typed, auto-listens again. Great for long dictation.');
        this._addInfoRow(comboGroup, 'AI only',
            'Click badge, speak, AI thinks and responds aloud, done.');
        this._addInfoRow(comboGroup, 'AI + Continuous (Hands-Free)',
            'Click badge, speak, AI responds, auto-listens, loop. Full voice assistant. Enable via panel menu Hands-Free toggle.');
        this._addInfoRow(comboGroup, 'Talk Mode (D-Bus)',
            'External apps call org.gnome.Speaks.Talk(text) for STT + LLM + TTS. Used by Claude Code, Copilot CLI, and MCP servers.');

        // ── Shortcuts ──
        const shortcutsGroup = new Adw.PreferencesGroup({
            title: 'Keyboard Shortcuts',
            description: 'Global shortcuts available from any window. Customize on the Extension page.',
        });
        page.add(shortcutsGroup);

        this._addShortcutRow(shortcutsGroup, 'Toggle Listening', 'toggle-listening-shortcut');
        this._addShortcutRow(shortcutsGroup, 'Speak Clipboard', 'speak-clipboard-shortcut');
        this._addShortcutRow(shortcutsGroup, 'Read Selection', 'read-selection-shortcut');
        this._addShortcutRow(shortcutsGroup, 'Toggle Voice Quality', 'toggle-voice-quality-shortcut');

        window.add(page);
    }

    // ═══════════════════════════════════════════════════════════════════
    // Page 1 — Azure Service Configuration
    // ═══════════════════════════════════════════════════════════════════
    _addAzurePage(window) {
        const page = new Adw.PreferencesPage({
            title: 'Azure',
            icon_name: 'network-server-symbolic',
        });

        // ── Authentication ──
        const authGroup = new Adw.PreferencesGroup({
            title: 'Authentication',
            description: 'Azure Speech Services credentials. Get a key at portal.azure.com.',
        });
        page.add(authGroup);

        this._addPasswordRow(authGroup, 'API Key', 'key',
            'Azure Speech Services subscription key');

        this._addRegionCombo(authGroup, 'Region', 'region',
            'Primary region for STT and TTS', 'westus2');

        // ── Separate TTS Region ──
        const ttsGroup = new Adw.PreferencesGroup({
            title: 'TTS Region (Optional)',
            description: 'Use a different region for text-to-speech, e.g. eastus for DragonHD voices.',
        });
        page.add(ttsGroup);

        this._addRegionCombo(ttsGroup, 'TTS Region', 'tts_region',
            'Leave empty to use the primary region', '');

        this._addPasswordRow(ttsGroup, 'TTS API Key', 'tts_key',
            'Leave empty to use the primary key');

        window.add(page);
    }

    // ═══════════════════════════════════════════════════════════════════
    // Page — Cloud AI Providers (cloud-chat-assistant config)
    // ═══════════════════════════════════════════════════════════════════
    _addCloudAIPage(window) {
        const page = new Adw.PreferencesPage({
            title: 'Cloud AI',
            icon_name: 'weather-overcast-symbolic',
        });

        // ── Azure AI Foundry ──
        const azureAiGroup = new Adw.PreferencesGroup({
            title: 'Azure AI Foundry',
            description: 'Azure AI endpoint for serverless and deployed models (GPT, Grok, DeepSeek, Llama, Phi, etc.).',
        });
        page.add(azureAiGroup);

        this._addCcaEntryRow(azureAiGroup, 'Endpoint', 'endpoint', '',
            'Azure AI services endpoint URL');

        this._addCcaPasswordRow(azureAiGroup, 'API Key', 'api_key',
            'Azure AI API key');

        this._addCcaComboRow(azureAiGroup, 'Model Type', 'model_type', [
            ['bedrock', 'AWS Bedrock'],
            ['deployed', 'Azure Deployed'],
            ['serverless', 'Azure Serverless'],
            ['google', 'Google Vertex AI'],
        ], 'bedrock');

        this._addCcaEntryRow(azureAiGroup, 'Deployment', 'deployment', '',
            'Azure deployment name (for deployed models)');

        // ── AWS Bedrock ──
        const awsGroup = new Adw.PreferencesGroup({
            title: 'AWS Bedrock',
            description: 'AWS credentials for Claude, Nova, Llama 4, and other Bedrock models.',
        });
        page.add(awsGroup);

        this._addCcaPasswordRow(awsGroup, 'Access Key', 'aws_access_key',
            'AWS Access Key ID');

        this._addCcaPasswordRow(awsGroup, 'Secret Key', 'aws_secret_key',
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
        const googleGroup = new Adw.PreferencesGroup({
            title: 'Google Vertex AI',
            description: 'Google Cloud credentials for Gemini models.',
        });
        page.add(googleGroup);

        this._addCcaPasswordRow(googleGroup, 'API Key', 'google_api_key',
            'Google Cloud API key');

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

        // ── Generation Parameters ──
        const genGroup = new Adw.PreferencesGroup({
            title: 'Generation',
            description: 'LLM response generation parameters.',
        });
        page.add(genGroup);

        this._addCcaSpinRow(genGroup, 'Temperature', 'temperature',
            0.0, 2.0, 0.1, 1, 1.0,
            '0 = deterministic, 2 = maximum randomness');

        this._addCcaSpinRow(genGroup, 'Max Tokens', 'max_completion_tokens',
            64, 128000, 256, 0, 2048,
            'Maximum tokens in LLM response');

        this._addCcaComboRow(genGroup, 'Reasoning Effort', 'reasoning_effort', [
            ['low', 'Low'],
            ['medium', 'Medium'],
            ['high', 'High'],
        ], 'high');

        this._addCcaSpinRow(genGroup, 'Max Conversation Turns', 'conversation_max_turns',
            1, 500, 10, 0, 50,
            'History turns before auto-trimming');

        // ── Multi-Chat ──
        const multiGroup = new Adw.PreferencesGroup({
            title: 'Multi-Chat',
            description: 'Compare responses from multiple models simultaneously.',
        });
        page.add(multiGroup);

        this._addCcaSpinRow(multiGroup, 'Timeout (seconds)', 'multi_chat_timeout',
            5, 120, 5, 0, 15,
            'Per-model timeout for multi-chat queries');

        window.add(page);
    }

    // ═══════════════════════════════════════════════════════════════════
    // Page 2 — Voice & TTS
    // ═══════════════════════════════════════════════════════════════════
    _addVoicePage(window) {
        const page = new Adw.PreferencesPage({
            title: 'Voice',
            icon_name: 'audio-speakers-symbolic',
        });

        // ── HD Voice ──
        const hdGroup = new Adw.PreferencesGroup({
            title: 'HD Voice',
            description: 'High-quality DragonHD voice with natural prosody. Requires a supported region (e.g. eastus).',
        });
        page.add(hdGroup);

        this._addComboRow(hdGroup, 'HD Voice Name', 'voice', [
            ['en-US-Ava:DragonHDLatestNeural', 'Ava (DragonHD)'],
            ['en-US-Andrew:DragonHDLatestNeural', 'Andrew (DragonHD)'],
            ['en-US-Brian:DragonHDLatestNeural', 'Brian (DragonHD)'],
            ['en-US-Emma:DragonHDLatestNeural', 'Emma (DragonHD)'],
            ['en-US-Aria:DragonHDLatestNeural', 'Aria (DragonHD)'],
            ['en-US-Davis:DragonHDLatestNeural', 'Davis (DragonHD)'],
            ['en-US-Jenny:DragonHDLatestNeural', 'Jenny (DragonHD)'],
            ['en-US-Guy:DragonHDLatestNeural', 'Guy (DragonHD)'],
            ['en-US-Steffan:DragonHDLatestNeural', 'Steffan (DragonHD)'],
            ['en-US-Christopher:DragonHDLatestNeural', 'Christopher (DragonHD)'],
            ['en-US-Eric:DragonHDLatestNeural', 'Eric (DragonHD)'],
            ['en-US-Roger:DragonHDLatestNeural', 'Roger (DragonHD)'],
            ['en-US-Alloy:DragonHDLatestNeural', 'Alloy (DragonHD)'],
            ['en-US-Echo:DragonHDLatestNeural', 'Echo (DragonHD)'],
            ['en-US-Fable:DragonHDLatestNeural', 'Fable (DragonHD)'],
            ['en-US-Onyx:DragonHDLatestNeural', 'Onyx (DragonHD)'],
            ['en-US-Nova:DragonHDLatestNeural', 'Nova (DragonHD)'],
            ['en-US-Shimmer:DragonHDLatestNeural', 'Shimmer (DragonHD)'],
        ], 'en-US-Ava:DragonHDLatestNeural');

        // ── Fast Voice ──
        const fastGroup = new Adw.PreferencesGroup({
            title: 'Fast Voice',
            description: 'Low-latency neural voice for quick responses (~120ms).',
        });
        page.add(fastGroup);

        this._addComboRow(fastGroup, 'Fast Voice Name', 'fast_voice', [
            ['en-US-AvaNeural', 'Ava'],
            ['en-US-AndrewNeural', 'Andrew'],
            ['en-US-AriaNeural', 'Aria'],
            ['en-US-DavisNeural', 'Davis'],
            ['en-US-JennyNeural', 'Jenny'],
            ['en-US-GuyNeural', 'Guy'],
            ['en-US-BrianNeural', 'Brian'],
            ['en-US-EmmaNeural', 'Emma'],
            ['en-US-SteffanNeural', 'Steffan'],
            ['en-US-ChristopherNeural', 'Christopher'],
            ['en-US-EricNeural', 'Eric'],
            ['en-US-RogerNeural', 'Roger'],
            ['en-US-MichelleNeural', 'Michelle'],
            ['en-US-MonicaNeural', 'Monica'],
            ['en-US-CoraNeural', 'Cora'],
            ['en-US-JaneNeural', 'Jane'],
            ['en-US-NancyNeural', 'Nancy'],
            ['en-US-SaraNeural', 'Sara'],
            ['en-US-TonyNeural', 'Tony'],
            ['en-US-JasonNeural', 'Jason'],
            ['en-US-BrandonNeural', 'Brandon'],
            ['en-US-JacobNeural', 'Jacob'],
            ['en-US-AmberNeural', 'Amber'],
            ['en-US-AshleyNeural', 'Ashley'],
            ['en-US-ElizabethNeural', 'Elizabeth'],
        ], 'en-US-AvaNeural');

        // ── Speech Parameters ──
        const paramGroup = new Adw.PreferencesGroup({
            title: 'Speech Parameters',
        });
        page.add(paramGroup);

        this._addSpinRow(paramGroup, 'Speed', 'speed',
            0.5, 3.0, 0.1, 1, 1.0);

        this._addComboRow(paramGroup, 'Pitch', 'pitch', [
            ['default', 'Default'],
            ['x-low', 'Extra Low'],
            ['low', 'Low'],
            ['medium', 'Medium'],
            ['high', 'High'],
            ['x-high', 'Extra High'],
        ], 'default');

        this._addComboRow(paramGroup, 'Volume', 'volume', [
            ['default', 'Default'],
            ['silent', 'Silent'],
            ['x-soft', 'Extra Soft'],
            ['soft', 'Soft'],
            ['medium', 'Medium'],
            ['loud', 'Loud'],
            ['x-loud', 'Extra Loud'],
        ], 'default');

        window.add(page);
    }

    // ═══════════════════════════════════════════════════════════════════
    // Page 3 — Listening / STT
    // ═══════════════════════════════════════════════════════════════════
    _addListeningPage(window) {
        const page = new Adw.PreferencesPage({
            title: 'Listening',
            icon_name: 'audio-input-microphone-symbolic',
        });

        // ── Timing ──
        const timingGroup = new Adw.PreferencesGroup({
            title: 'Timing',
            description: 'Control when recording starts and stops.',
        });
        page.add(timingGroup);

        this._addSpinRow(timingGroup, 'Talk Silence Timeout', 'talk_silence_timeout',
            0.5, 10.0, 0.5, 1, 4.0,
            'Silence timeout for talk/converse mode (D-Bus Talk calls)');

        this._addSpinRow(timingGroup, 'Max Record Seconds', 'max_record_seconds',
            5, 300, 5, 0, 120,
            'Absolute maximum recording duration');

        // ── Detection ──
        const detectGroup = new Adw.PreferencesGroup({
            title: 'Detection',
            description: 'Fine-tune speech detection sensitivity.',
        });
        page.add(detectGroup);

        this._addSpinRow(detectGroup, 'VAD Aggressiveness', 'vad_aggressiveness',
            0, 3, 1, 0, 3,
            '0 = least aggressive, 3 = most aggressive noise rejection');

        this._addSpinRow(detectGroup, 'Energy Multiplier', 'energy_multiplier',
            0.5, 20.0, 0.5, 1, 2.5,
            'Noise gate threshold multiplier. Lower = more sensitive to quiet speech');

        // ── Language ──
        const langGroup = new Adw.PreferencesGroup({
            title: 'Language',
        });
        page.add(langGroup);

        this._addEntryRow(langGroup, 'STT Language', 'language', 'en-US',
            'BCP-47 language code for speech recognition (e.g. en-US, de-DE, ja-JP)');

        window.add(page);
    }

    // ═══════════════════════════════════════════════════════════════════
    // Page 4 — Audio Devices
    // ═══════════════════════════════════════════════════════════════════
    _addAudioPage(window) {
        const page = new Adw.PreferencesPage({
            title: 'Audio',
            icon_name: 'audio-card-symbolic',
        });

        // Enumerate PipeWire sinks and sources for device menus
        const sinks = this._enumeratePipeWireDevices('sinks');
        const sources = this._enumeratePipeWireDevices('sources');

        // ── Playback ──
        const playGroup = new Adw.PreferencesGroup({
            title: 'Playback',
        });
        page.add(playGroup);

        const playerOptions = [['auto', 'Auto-detect']];
        for (const [cmd, label] of [
            ['aplay', 'aplay (ALSA)'],
            ['pw-play', 'pw-play (PipeWire)'],
            ['pw-cat', 'pw-cat (PipeWire)'],
            ['ffplay', 'ffplay (FFmpeg)'],
        ]) {
            if (this._commandExists(cmd))
                playerOptions.push([cmd, label]);
            else
                playerOptions.push([cmd, `${label} — not found`]);
        }
        this._addComboRow(playGroup, 'Player', 'player', playerOptions, 'auto');

        this._addComboRow(playGroup, 'Speaker', 'speaker_sink',
            [['', 'System Default'], ...sinks], '');

        // ── Recording ──
        const recGroup = new Adw.PreferencesGroup({
            title: 'Recording',
        });
        page.add(recGroup);

        const recorderOptions = [['auto', 'Auto-detect']];
        for (const [cmd, label] of [
            ['pw-record', 'pw-record (PipeWire)'],
            ['arecord', 'arecord (ALSA)'],
        ]) {
            if (this._commandExists(cmd))
                recorderOptions.push([cmd, label]);
            else
                recorderOptions.push([cmd, `${label} — not found`]);
        }
        this._addComboRow(recGroup, 'Recorder', 'recorder', recorderOptions, 'auto');

        this._addComboRow(recGroup, 'Microphone', 'mic_source',
            [['', 'System Default'], ...sources], '');

        // ── Duplex ──
        const duplexGroup = new Adw.PreferencesGroup({
            title: 'Duplex Mode',
            description: 'Controls whether TTS and STT can run simultaneously.',
        });
        page.add(duplexGroup);

        this._addComboRow(duplexGroup, 'Half Duplex', 'half_duplex', [
            ['auto', 'Auto (speakers→half, headphones→full)'],
            ['true', 'Force Half Duplex (speak then listen)'],
            ['false', 'Force Full Duplex (simultaneous)'],
        ], 'auto');

        window.add(page);
    }

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

            // Find the section header: "Sinks:" or "Sources:" within the Audio block
            const header = type === 'sinks' ? 'Sinks:' : 'Sources:';
            let inAudio = false;
            let inSection = false;

            for (const line of lines) {
                const trimmed = line.trim();

                // Track whether we are inside the "Audio" top-level block so
                // we don't accidentally match Video sinks/sources.
                if (trimmed === 'Audio') {
                    inAudio = true;
                    continue;
                }
                if (inAudio && /^(Video|Settings)$/.test(trimmed)) {
                    break;           // left the Audio block
                }
                if (!inAudio) continue;

                if (trimmed.endsWith(header)) {
                    inSection = true;
                    continue;
                }

                // Stop at next sub-section (empty pipe line, blank line, or a
                // new header such as "Sink endpoints:" / "Streams:")
                if (inSection && (trimmed === '│' || trimmed === '' ||
                    (trimmed.endsWith(':') && !trimmed.match(/^\d/)))) {
                    break;
                }

                if (!inSection) continue;

                // Strip leading box-drawing characters (│ ├ └ ─ etc.) so the
                // regex only sees the device text, e.g.:
                //   "│      35. USB Audio Device Analog Stereo      [vol: 0.19]"
                //   "│  *   55. Built-in Audio Analog Stereo        [vol: 0.65]"
                const stripped = trimmed.replace(/^[│├└─┬┤┼╌╎\s]+/, '');

                // Match: optional "*", node ID, name, and optional "[...]" tail
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

    /**
     * Check if a command exists on the system (uses `which`).
     */
    _commandExists(cmd) {
        try {
            const [ok, stdout, stderr, exitCode] = GLib.spawn_command_line_sync(`which ${cmd}`);
            return ok && exitCode === 0;
        } catch (e) {
            return false;
        }
    }

    // ═══════════════════════════════════════════════════════════════════
    // Page — Spellcraft (wake word, spellbook, offline fallback)
    // ═══════════════════════════════════════════════════════════════════
    _addSpellcraftPage(window) {
        const page = new Adw.PreferencesPage({
            title: 'Spellcraft',
            icon_name: 'weather-clear-night-symbolic',
        });

        // ── Wake Word ──
        const wakeGroup = new Adw.PreferencesGroup({
            title: 'Wake Word',
            description: 'Hands-free activation via a Wyoming openwakeword server on your LAN. ' +
                'Speaking your wake phrase while idle opens the mic exactly like the keyboard shortcut. ' +
                'Never armed during dictation or TTS playback.',
        });
        page.add(wakeGroup);

        this._addSwitchRow(wakeGroup, 'Enable Wake Word',
            'Stream mic audio to the wake server while idle ("cast wake word" toggles this by voice)',
            'wake_word', false);

        this._addEntryRow(wakeGroup, 'Wake Model', 'wake_word_model', '',
            'openwakeword model name — effectively your wake phrase; treat it as private');

        this._addSpinRow(wakeGroup, 'Wake Server Port', 'wyoming_wake_port',
            1, 65535, 1, 0, 10400,
            'Wyoming openwakeword port on the fallback host below');

        // ── Voice Spellbook ──
        const spellGroup = new Adw.PreferencesGroup({
            title: 'Voice Spellbook',
            description: 'Utterances starting with "cast" or "invoke" run local spells instead of ' +
                'being typed or sent to the LLM: mode switching, realm scrying, home rituals, oracle ' +
                'consultations. Spells are JSON — the extension ships the self-control set; add your ' +
                'own in the overlay below (hot-reloads on save). Test without a mic: ' +
                'POST /cast on localhost:7710.',
        });
        page.add(spellGroup);

        const overlayRow = new Adw.ActionRow({
            title: 'User Spell Overlay',
            subtitle: '~/.config/speech-to-cli/spellbook.json',
        });
        overlayRow.add_suffix(new Gtk.Image({ icon_name: 'document-edit-symbolic' }));
        overlayRow.activatable = true;
        overlayRow.connect('activated', () => {
            const path = GLib.get_home_dir() + '/.config/speech-to-cli/spellbook.json';
            Gio.AppInfo.launch_default_for_uri(`file://${path}`, null);
        });
        spellGroup.add(overlayRow);

        // ── Offline Fallback ──
        const offlineGroup = new Adw.PreferencesGroup({
            title: 'Offline Fallback',
            description: 'When Azure is unreachable, speech falls back to a Wyoming server on your ' +
                'LAN (Piper TTS + local STT). A 60-second circuit breaker skips Azure while it is ' +
                'down. Leave the host empty to disable.',
        });
        page.add(offlineGroup);

        this._addEntryRow(offlineGroup, 'Wyoming Host', 'wyoming_host', '',
            'LAN hostname or IP of the Wyoming server (empty = feature off)');

        this._addSpinRow(offlineGroup, 'TTS Port', 'wyoming_tts_port',
            1, 65535, 1, 0, 10200,
            'Wyoming TTS service (e.g. wyoming-piper)');

        this._addSpinRow(offlineGroup, 'STT Port', 'wyoming_stt_port',
            1, 65535, 1, 0, 10300,
            'Wyoming STT service (e.g. wyoming-onnx-asr)');

        this._addEntryRow(offlineGroup, 'Offline Voice', 'wyoming_tts_voice', 'en_GB-cori-high',
            'Piper voice name (empty = server default)');

        window.add(page);
    }

    // ═══════════════════════════════════════════════════════════════════
    // Page 5 — Feedback (Chimes & Visual)
    // ═══════════════════════════════════════════════════════════════════
    _addFeedbackPage(window) {
        const page = new Adw.PreferencesPage({
            title: 'Feedback',
            icon_name: 'preferences-desktop-notifications-symbolic',
        });

        // ── Sound Chimes ──
        const chimeGroup = new Adw.PreferencesGroup({
            title: 'Sound Chimes',
            description: 'Audio feedback cues during speech operations.',
        });
        page.add(chimeGroup);

        this._addSwitchRow(chimeGroup, 'Ready Chime',
            'Play ascending tone when microphone opens',
            'chime_ready', true);

        this._addSwitchRow(chimeGroup, 'Processing Chime',
            'Play blip when speech is recognized',
            'chime_processing', false);

        this._addSwitchRow(chimeGroup, 'Speak Chime',
            'Play descending tone before TTS starts',
            'chime_speak', false);

        this._addSwitchRow(chimeGroup, 'Done Chime',
            'Play double-tap tone when TTS finishes',
            'chime_done', false);

        this._addSwitchRow(chimeGroup, 'Thinking Hum',
            'Play looping 150Hz hum while processing',
            'chime_hum', false);

        // ── Visual Feedback ──
        const visualGroup = new Adw.PreferencesGroup({
            title: 'Visual Feedback',
        });
        page.add(visualGroup);

        this._addGSettingsSwitchRow(visualGroup, 'Live Subtitles',
            'Show real-time transcription text',
            'live-subtitles');

        this._addSwitchRow(visualGroup, 'Waveform Bars',
            'Show audio level waveform below badge',
            'show_waveform', true);

        this._addSwitchRow(visualGroup, 'VAD Indicator',
            'Show green dot when speech is detected',
            'show_vad_dot', true);

        this._addSwitchRow(visualGroup, 'Silence Fade',
            'Waveform dims during extended silence',
            'show_silence_fade', true);

        this._addSwitchRow(visualGroup, 'Badge Pulse',
            'Breathing animation when listening or speaking',
            'show_badge_pulse', true);

        this._addSwitchRow(visualGroup, 'Badge Audio Scale',
            'Badge grows with voice volume',
            'show_badge_scale', true);

        this._addSwitchRow(visualGroup, 'Word Highlights',
            'New words flash blue in subtitles',
            'show_word_highlights', true);

        this._addSwitchRow(visualGroup, 'VU Meter',
            'Show volume meter animation during audio',
            'vu_meter', true);

        this._addSwitchRow(visualGroup, 'Visual Indicator',
            'Show status icons in terminal',
            'visual_indicator', true);

        // ── Subtitle Colors ──
        const colorGroup = new Adw.PreferencesGroup({
            title: 'Subtitle Colors',
        });
        page.add(colorGroup);

        this._addComboRow(colorGroup, 'User Speech Color', 'subtitle_color_user',
            SUBTITLE_COLORS, 'light_green');

        this._addComboRow(colorGroup, 'TTS Speech Color', 'subtitle_color_tts',
            SUBTITLE_COLORS, 'amber');

        window.add(page);
    }

    // ═══════════════════════════════════════════════════════════════════
    // Page 6 — Extension Settings (GSettings)
    // ═══════════════════════════════════════════════════════════════════
    _addExtensionPage(window) {
        const page = new Adw.PreferencesPage({
            title: 'Extension',
            icon_name: 'preferences-system-symbolic',
        });

        // ── Badge ──
        const badgeGroup = new Adw.PreferencesGroup({
            title: 'Floating Badge',
            description: 'The floating voice-status badge on your desktop.',
        });
        page.add(badgeGroup);

        this._addGSettingsSwitchRow(badgeGroup, 'Show Badge',
            'Display the floating badge on the desktop',
            'show-badge');

        this._addGSettingsSpinRow(badgeGroup, 'Position X', 'badge-position-x',
            -1, 5000, 1, 0,
            '-1 for auto-center');

        this._addGSettingsSpinRow(badgeGroup, 'Position Y', 'badge-position-y',
            -1, 5000, 1, 0,
            '-1 for auto-position at bottom');

        // ── Panel ──
        const panelGroup = new Adw.PreferencesGroup({
            title: 'Panel Indicator',
        });
        page.add(panelGroup);

        this._addGSettingsSwitchRow(panelGroup, 'Show Panel Indicator',
            'Display GNOME Speaks in the top panel',
            'show-panel-indicator');

        // ── Keyboard Shortcuts ──
        const shortcutGroup = new Adw.PreferencesGroup({
            title: 'Keyboard Shortcuts',
            description: 'Global shortcuts. Change via GNOME Settings > Keyboard > Custom Shortcuts, or edit dconf directly.',
        });
        page.add(shortcutGroup);

        this._addShortcutRow(shortcutGroup, 'Toggle Listening', 'toggle-listening-shortcut');
        this._addShortcutRow(shortcutGroup, 'Speak Clipboard', 'speak-clipboard-shortcut');
        this._addShortcutRow(shortcutGroup, 'Read Selection', 'read-selection-shortcut');
        this._addShortcutRow(shortcutGroup, 'Toggle Voice Quality', 'toggle-voice-quality-shortcut');

        window.add(page);
    }

    // ═══════════════════════════════════════════════════════════════════
    // Page 7 — Advanced
    // ═══════════════════════════════════════════════════════════════════
    _addAdvancedPage(window) {
        const page = new Adw.PreferencesPage({
            title: 'Advanced',
            icon_name: 'preferences-other-symbolic',
        });

        // ── Debug ──
        const debugGroup = new Adw.PreferencesGroup({
            title: 'Debug',
        });
        page.add(debugGroup);

        this._addSwitchRow(debugGroup, 'Debug Mode',
            'Write detailed logs to /tmp/speech-debug.log',
            'debug', false);

        // ── Barge-in Details ──
        const bargeGroup = new Adw.PreferencesGroup({
            title: 'Barge-in Details',
            description: 'Fine-tune barge-in behavior (enable on the Modes page).',
        });
        page.add(bargeGroup);

        this._addSpinRow(bargeGroup, 'Barge-in Frames', 'barge_in_frames',
            1, 20, 1, 0, 3,
            'Speech frames needed to trigger barge-in');

        this._addSpinRow(bargeGroup, 'Barge-in Silence', 'barge_in_silence',
            0.3, 10.0, 0.1, 1, 1.0,
            'Seconds of silence before resuming TTS');

        this._addSwitchRow(bargeGroup, 'Barge-in Chime',
            'Play chime when barge-in is detected',
            'chime_barge_in', true);

        // ── Auto-Corrections ──
        const correctGroup = new Adw.PreferencesGroup({
            title: 'Auto-Corrections',
            description: 'Custom word replacements applied to transcriptions. Format: wrong=right, one per line.',
        });
        page.add(correctGroup);

        this._addCorrectionsRow(correctGroup);

        // ── Other ──
        const otherGroup = new Adw.PreferencesGroup({
            title: 'Other',
        });
        page.add(otherGroup);

        this._addSwitchRow(otherGroup, 'Echo Cancellation',
            'Use PipeWire echo cancellation nodes if available',
            'enable_echo_cancel', true);

        this._addSwitchRow(otherGroup, 'Enable Pause',
            'Allow pausing and resuming playback',
            'enable_pause', true);

        // ── Service Management ──
        const serviceGroup = new Adw.PreferencesGroup({
            title: 'Service',
            description: 'Restart to apply config.json changes to the running speech service.',
        });
        page.add(serviceGroup);

        const restartRow = new Adw.ActionRow({
            title: 'Restart Speech Service',
            subtitle: 'systemctl --user restart gnome-speaks.service',
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
    // Widget Helpers — config.json backed
    // ═══════════════════════════════════════════════════════════════════

    _addInfoRow(group, title, subtitle) {
        const row = new Adw.ActionRow({
            title: title,
            subtitle: subtitle,
            subtitle_lines: 3,
        });
        group.add(row);
        return row;
    }

    _addComboRow(group, title, configKey, options, defaultValue) {
        // options: [[value, label], ...]
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

        // Add current value if custom (not in preset list)
        if (currentValue && !regions.find(r => r[0] === currentValue))
            regions.push([currentValue, `${currentValue} (custom)`]);

        // For optional fields, add "none" option
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

    _addEntryRow(group, title, configKey, defaultValue, description) {
        const currentValue = this._config[configKey] ?? defaultValue;
        const row = new Adw.EntryRow({
            title: title,
            text: currentValue != null ? String(currentValue) : '',
            show_apply_button: true,
        });

        if (description) {
            // Wrap in an ExpanderRow for description, or use group description
            // For simplicity, show as subtitle via ActionRow approach
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

    _addPasswordRow(group, title, configKey, subtitle) {
        const currentValue = this._config[configKey] || '';

        let row;
        try {
            row = new Adw.PasswordEntryRow({
                title: title,
                text: currentValue,
                show_apply_button: true,
            });
        } catch (e) {
            // Fallback for older Adw without PasswordEntryRow
            row = new Adw.EntryRow({
                title: title,
                text: currentValue,
                show_apply_button: true,
            });
        }

        row.connect('apply', () => {
            const text = row.get_text().trim();
            if (text === '')
                this._deleteConfigKey(configKey);
            else
                this._setConfigValue(configKey, text);
        });

        if (subtitle) {
            const wrapper = new Adw.PreferencesGroup({description: subtitle});
            wrapper.add(row);
            group.add(wrapper);
            return row;
        }

        group.add(row);
        return row;
    }

    _addCorrectionsRow(group) {
        const corrections = this._config['auto_corrections'] || {};
        const text = Object.entries(corrections)
            .map(([wrong, right]) => `${wrong}=${right}`)
            .join('\n');

        const row = new Adw.ActionRow({
            title: 'Edit Corrections',
            subtitle: `${Object.keys(corrections).length} corrections defined`,
        });

        const editButton = new Gtk.Button({
            label: 'Edit',
            valign: Gtk.Align.CENTER,
        });

        editButton.connect('clicked', () => {
            const dialog = new Gtk.Dialog({
                title: 'Auto-Corrections',
                modal: true,
                default_width: 400,
                default_height: 300,
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
            textView.buffer.set_text(text, -1);

            const scrolled = new Gtk.ScrolledWindow({
                child: textView,
                vexpand: true,
                hexpand: true,
            });

            const box = dialog.get_content_area();
            const label = new Gtk.Label({
                label: 'One correction per line: wrong=right',
                halign: Gtk.Align.START,
                margin_start: 8,
                margin_top: 8,
            });
            box.append(label);
            box.append(scrolled);

            dialog.add_button('Cancel', Gtk.ResponseType.CANCEL);
            dialog.add_button('Save', Gtk.ResponseType.OK);

            dialog.connect('response', (dlg, response) => {
                if (response === Gtk.ResponseType.OK) {
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
                    row.subtitle = `${Object.keys(newCorrections).length} corrections defined`;
                }
                dlg.destroy();
            });

            dialog.present();
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
            const dialog = new Adw.MessageDialog({
                heading: `Set shortcut for "${title}"`,
                body: 'Enter a keyboard shortcut (e.g. <Super><Alt>space):',
                modal: true,
            });

            const entry = new Gtk.Entry({
                text: currentShortcut,
                margin_start: 16,
                margin_end: 16,
                margin_bottom: 8,
            });
            dialog.set_extra_child(entry);

            dialog.add_response('cancel', 'Cancel');
            dialog.add_response('save', 'Save');
            dialog.add_response('disable', 'Disable');
            dialog.set_response_appearance('save', Adw.ResponseAppearance.SUGGESTED);

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

            dialog.present();
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
        const json = JSON.stringify(this._config, null, 2) + '\n';
        const encoder = new TextEncoder();
        GLib.file_set_contents(path, encoder.encode(json));
    }

    _setConfigValue(key, value) {
        this._config[key] = value;
        this._scheduleConfigSave();
    }

    _deleteConfigKey(key) {
        delete this._config[key];
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
        const json = JSON.stringify(this._ccaConfig, null, 2) + '\n';
        const encoder = new TextEncoder();
        GLib.file_set_contents(path, encoder.encode(json));
    }

    _setCcaConfigValue(key, value) {
        this._ccaConfig[key] = value;
        this._scheduleCcaConfigSave();
    }

    _deleteCcaConfigKey(key) {
        delete this._ccaConfig[key];
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

    _addCcaEntryRow(group, title, configKey, defaultValue, description) {
        const currentValue = this._ccaConfig[configKey] ?? defaultValue;
        const row = new Adw.EntryRow({
            title: title,
            text: currentValue != null ? String(currentValue) : '',
            show_apply_button: true,
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

    _addCcaPasswordRow(group, title, configKey, subtitle) {
        const currentValue = this._ccaConfig[configKey] || '';

        let row;
        try {
            row = new Adw.PasswordEntryRow({
                title: title,
                text: currentValue,
                show_apply_button: true,
            });
        } catch (e) {
            row = new Adw.EntryRow({
                title: title,
                text: currentValue,
                show_apply_button: true,
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
        button.set_label('Restarting...');

        try {
            const [ok] = GLib.spawn_command_line_async(
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
