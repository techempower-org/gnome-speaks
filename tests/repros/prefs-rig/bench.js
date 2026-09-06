import GLib from 'gi://GLib';
const CMDS = ['aplay','pw-play','pw-cat','ffplay','pw-record','arecord'];
const ms = t => (t/1000).toFixed(1);
for (let run = 1; run <= 3; run++) {
    let t = GLib.get_monotonic_time();
    for (let i = 0; i < 2; i++) GLib.spawn_command_line_sync('wpctl status');
    const wpctl = GLib.get_monotonic_time() - t;
    t = GLib.get_monotonic_time();
    for (const c of CMDS) GLib.spawn_command_line_sync(`which ${c}`);
    const which = GLib.get_monotonic_time() - t;
    t = GLib.get_monotonic_time();
    for (const c of CMDS) GLib.find_program_in_path(c);
    const fpip = GLib.get_monotonic_time() - t;
    print(`run${run}  OLD blocking: 2x wpctl=${ms(wpctl)}ms + 6x which=${ms(which)}ms = ${ms(wpctl+which)}ms   |   NEW blocking: find_program_in_path x6=${ms(fpip)}ms + wpctl async=0ms`);
}
