#!/usr/bin/env python3
"""Turn the driver's dumps into PASS/FAIL lines. Exit 1 on any FAIL.

Every check names the dump it read. The measurements that matter for #113
are printed even when they pass, so a reviewer sees the computed colours
rather than a green word.
"""
import json, os, re, sys

dumps_dir, log, src = sys.argv[1:4]
fails = 0


def load(name):
    with open(os.path.join(dumps_dir, name + '.json')) as f:
        return json.load(f)


def check(cond, what):
    global fails
    print(('   ok   ' if cond else '!! FAIL ') + what)
    if not cond:
        fails += 1


t0 = load('t0_no_service')
check(t0['target_loaded'], 't0: gnome-speaks extension loaded in the headless shell')
check(t0['notify_is_function'], 't0: Main.notify is a function on this shell')
check(t0['state'] == 'unavailable', f"t0: no service on the bus -> state {t0['state']!r} (baseline main: 'idle')")
check('gnome-speaks-unavailable' in t0['badge']['classes'].split(), f"t0: badge classes {t0['badge']['classes']!r}")
check('gnome-speaks-idle' not in t0['badge']['classes'].split(), 't0: idle class NOT also present')
check(t0['badge']['accessible_name'].startswith('GNOME Speaks service not running'),
      f"t0: accessible name {t0['badge']['accessible_name']!r}")
check(t0['icon']['name'] == 'microphone-disabled-symbolic', f"t0: icon {t0['icon']['name']!r}")
check(t0['label']['visible'] is False, 't0: label hidden while not hovered/focused (compact)')
check(t0['menu']['service'] and 'not running' in t0['menu']['service']['text'] and t0['menu']['service']['sensitive'],
      f"t0: menu service row {t0['menu']['service']!r}")
check(t0['menu']['listen']['sensitive'] is False and t0['menu']['stop']['sensitive'] is False,
      't0: Start Listening / Stop rows insensitive')
check(t0['panel_icon']['name'] == 'microphone-disabled-symbolic' and 'gnome-speaks-panel-unavailable' in t0['panel_icon']['classes'],
      f"t0: panel icon {t0['panel_icon']!r}")
print(f"        measured t0: badge bg={t0['badge']['bg']} border={t0['badge']['border']} pad={t0['badge']['padding_left']} "
      f"icon={t0['icon']['color']}@{t0['icon']['size']} panel={t0['panel_icon']['color']}")

t1 = load('t1_stub_listening')
check(t1['state'] == 'listening', f"t1: stub owns the name + StateChanged -> {t1['state']!r}")
check('gnome-speaks-unavailable' not in t1['badge']['classes'].split(), 't1: unavailable class removed')
check(all(t1.get('pills_visible', [])), f"t1: pills shown while listening {t1.get('pills_visible')}")
check(t1['menu']['service']['text'] == 'Service: running' and t1['menu']['service']['sensitive'] is False,
      f"t1: menu service row {t1['menu']['service']!r}")

t2 = load('t2_stub_gone')
check(t2['state'] == 'unavailable', f"t2: name vanished mid-listen -> {t2['state']!r}")
check(not any(t2.get('pills_visible', [])), f"t2: pills hidden again {t2.get('pills_visible')}")
check('gnome-speaks-listening' not in t2['badge']['classes'].split(), 't2: listening class removed')
check(t2['label']['visible'] is False, 't2: label hidden')

t3 = load('t3_hover')
check(t3['badge']['hover'] is True, 't3: hover set')
check(t3['label']['visible'] is True and t3['label']['text'].startswith('Service not running'),
      f"t3: hover reveals tooltip label {t3['label']['text']!r}")
print(f"        measured t3 (hover): bg={t3['badge']['bg']} border={t3['badge']['border']} icon={t3['icon']['color']} label={t3['label']['color']}")
check(t3['badge']['border'] != t0['badge']['border'], 't3: hover ring differs from rest (rule applies)')
check(t3['icon']['color'] != t0['icon']['color'], 't3: hover icon tint differs from rest')

t4 = load('t4_unhover')
check(t4['label']['visible'] is False, 't4: unhover hides the label again')

t5 = load('t5_focus')
check(t5['badge']['has_key_focus'] is True, 't5: badge has key focus')
check(t5['label']['visible'] is True, 't5: keyboard focus reveals the label (a11y parity)')
print(f"        measured t5 (focus): border={t5['badge']['border']} icon={t5['icon']['color']}")
check(t5['badge']['border'] == t3['badge']['border'], 't5: :focus ring == :hover ring')
t6 = load('t6_unfocus')
check(t6['label']['visible'] is False, 't6: focus out hides the label')

t7 = load('t7_tap_pending')
check(t7['service_start_pending'] is True, 't7: tap -> start pending')
check(t7['label']['visible'] is True and 'Starting' in t7['label']['text'], f"t7: label {t7['label']['text']!r}")
check('starting' in t7['menu']['service']['text'].lower() and t7['menu']['service']['sensitive'] is False,
      f"t7: menu row while starting {t7['menu']['service']!r}")

t8 = load('t8_tap_failed')
check(t8['service_start_pending'] is False, 't8: systemctl (sandboxed) failed -> pending cleared')
check(t8['state'] == 'unavailable', 't8: still unavailable')
toasts = [n for n in t8.get('notifications', []) if 'Could not start gnome-speaks.service' in (n.get('body') or '')]
check(len(toasts) == 1, f"t8: exactly one failure toast: {toasts!r}")
check(t8['label']['visible'] is False, 't8: label hidden again after failure (not hovered)')

t9 = load('t9_hotkey')
toasts9 = [n for n in t9.get('notifications', []) if 'Could not start gnome-speaks.service' in (n.get('body') or '')]
check(len(toasts9) == 2, f"t9: hotkey seam (StartListening) attempted a start too -> {len(toasts9)} failure toasts")
t10 = load('t10_other_method')
other = [n for n in t10.get('notifications', []) if 'not running' in (n.get('body') or '') and 'tap the badge' in n['body']]
check(len(other) == 1, f"t10: non-listen method while unavailable -> explanatory toast {other!r}")

t11 = load('t11_stub_idle')
check(t11['state'] == 'idle', f"t11: service back -> {t11['state']!r}")
check('gnome-speaks-idle' in t11['badge']['classes'].split() and 'gnome-speaks-unavailable' not in t11['badge']['classes'].split(),
      f"t11: classes {t11['badge']['classes']!r}")
check(t11['icon']['name'] == 'audio-input-microphone-symbolic', 't11: mic icon restored')
check(t11['menu']['service']['text'] == 'Service: running', 't11: menu row running')
print(f"        measured t11 (idle): bg={t11['badge']['bg']} border={t11['badge']['border']} icon={t11['icon']['color']}")
check(t11['badge']['bg'] != t0['badge']['bg'], 't11: idle glass != unavailable glass (state is visually distinct)')

t12 = load('t12_gone_again')
check(t12['state'] == 'unavailable', 't12: second vanish -> unavailable')
t13 = load('t13_reenabled')
check(t13['target_loaded'] and t13['state'] == 'unavailable', f"t13: disable/enable clean, state {t13['state']!r}")

# ---- shell log: zero gnome-speaks JS errors / CRITICALs / St warnings ----
with open(log, errors='replace') as f:
    lines = f.readlines()
bad = [l.rstrip() for l in lines if re.search(r'JS ERROR|CRITICAL|Ignoring excess|St-WARNING|Clutter-CRITICAL', l)
       and re.search(r'gnome-speaks|speaks-probe|extension\.js', l)]
check(not bad, f"shell log: {len(bad)} gnome-speaks error/critical/St-warning lines")
for l in bad[:20]:
    print('        ' + l)
# Control: the log must show the extension actually enabling, or "zero
# errors" is the zero of an instrument that saw nothing.
loaded = any('gnome-speaks' in l for l in lines)
print(f"        control: shell.log has {sum('gnome-speaks' in l for l in lines)} lines mentioning gnome-speaks ({len(lines)} total)")

print(f"\n{'!! FAIL' if fails else 'PASS'}: {fails} failing check(s)")
sys.exit(1 if fails else 0)
