# Barbell rep counter: behaviour model of the firmware (runs in SimWorld's behavior backend).
# Counts one rep per bottom turn-around of the bar, seen as a positive peak of vertical acceleration
# after a negative one (hysteresis); weight from the knob; mode button cycles the exercise;
# export button writes the session as CSV (what the phone app would import over BLE).
EXERCISES = ['bench', 'press', 'pull', 'squat']
G = 9.80665


def setup(io):
    s = io.state
    s.update(reps=0, armed=False, lp=0.0, sets=[], ex=0, last_btn=1, last_mode=1, set_start=None,
             last_rep_t=-10.0)
    io.log('rep counter ready')


def loop(io, t):
    s = io.state
    a = io.imu('imu')['az'] - G                 # vertical, gravity removed (sensor upright on the bar)
    s['lp'] += (a - s['lp']) * 0.05             # ~8 Hz low-pass at 1 kHz
    th = io.params.get('threshold_ms2', 0.6)
    if s['lp'] < -th:
        s['armed'] = True
    elif s['lp'] > th and s['armed'] and t - s['last_rep_t'] > 0.4:
        s['armed'] = False
        s['reps'] += 1
        s['last_rep_t'] = t
        if s['set_start'] is None:
            s['set_start'] = t
        io.event('rep', n=s['reps'])
    # a set ends after 3 s without reps
    if s['reps'] and t - s['last_rep_t'] > 3.0:
        close_set(io, t)
    mode = io.read('GPIO39')
    if mode == 0 and s['last_mode'] == 1:
        s['ex'] = (s['ex'] + 1) % len(EXERCISES)
        io.event('mode', exercise=EXERCISES[s['ex']])
    s['last_mode'] = mode
    btn = io.read('GPIO37')
    if btn == 0 and s['last_btn'] == 1:
        if s['reps']:
            close_set(io, t)
        io.export(s['sets'], name='session.csv')
        io.log(f"exported {len(s['sets'])} set(s)")
    s['last_btn'] = btn


def close_set(io, t):
    s = io.state
    kg = round(io.adc('GPIO36') / 4095 * 200 / 2.5) * 2.5     # knob: 0-200 kg in 2.5 kg steps
    s['sets'].append({'set': len(s['sets']) + 1, 'exercise': EXERCISES[s['ex']], 'reps': s['reps'],
                      'weight_kg': kg, 't_start_s': round(s['set_start'], 2), 't_end_s': round(s['last_rep_t'], 2)})
    io.event('set', exercise=EXERCISES[s['ex']], reps=s['reps'], kg=kg)
    s['reps'], s['set_start'] = 0, None
