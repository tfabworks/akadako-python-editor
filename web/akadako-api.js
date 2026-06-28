// AkaDako API catalogue used by the editor's autocomplete.
// Each entry: name, sig (shown), doc (JP one-liner), insert (text inserted).
// `insert` ends with "()" for no-arg calls, or "(" when arguments are expected.

const BOARD_API = [
  // --- 接続 ---
  ["disconnect", "disconnect()", "ボードを切断する", "disconnect()"],
  ["is_connected", "is_connected", "接続中かどうか (プロパティ)", "is_connected"],

  // --- I2Cセンサー ---
  ["fetch_temperature", "fetch_temperature() -> float", "温度 (℃)", "fetch_temperature()"],
  ["fetch_humidity", "fetch_humidity() -> float", "湿度 (%)", "fetch_humidity()"],
  ["fetch_pressure", "fetch_pressure() -> float", "気圧 (hPa)", "fetch_pressure()"],
  ["fetch_brightness", "fetch_brightness() -> float", "明るさ I2C (lx)", "fetch_brightness()"],
  ["fetch_optical_distance", "fetch_optical_distance() -> float", "レーザー距離 (cm)", "fetch_optical_distance()"],
  ["fetch_water_temperature_a", "fetch_water_temperature_a() -> float", "水温 Digital A (℃)", "fetch_water_temperature_a()"],
  ["fetch_acceleration_x", "fetch_acceleration_x() -> float", "加速度 X (m/s^2)", "fetch_acceleration_x()"],
  ["fetch_acceleration_y", "fetch_acceleration_y() -> float", "加速度 Y (m/s^2)", "fetch_acceleration_y()"],
  ["fetch_acceleration_z", "fetch_acceleration_z() -> float", "加速度 Z (m/s^2)", "fetch_acceleration_z()"],
  ["fetch_acceleration_magnitude", "fetch_acceleration_magnitude() -> float", "加速度の大きさ", "fetch_acceleration_magnitude()"],
  ["fetch_pitch", "fetch_pitch() -> float", "ピッチ角 (度)", "fetch_pitch()"],
  ["fetch_roll", "fetch_roll() -> float", "ロール角 (度)", "fetch_roll()"],

  // --- アナログ入力 (0-100%) ---
  ["analog_a1", "analog_a1() -> float", "アナログ A1 (0-100%)", "analog_a1()"],
  ["analog_a2", "analog_a2() -> float", "アナログ A2 (0-100%)", "analog_a2()"],
  ["analog_b1", "analog_b1() -> float", "アナログ B1 (0-100%)", "analog_b1()"],
  ["analog_b2", "analog_b2() -> float", "アナログ B2 (0-100%)", "analog_b2()"],
  ["analog_brightness", "analog_brightness() -> float", "内蔵アナログ照度 (B2)", "analog_brightness()"],

  // --- デジタル入力 (bool) ---
  ["digital_a1", "digital_a1() -> bool", "デジタル A1", "digital_a1()"],
  ["digital_a2", "digital_a2() -> bool", "デジタル A2", "digital_a2()"],
  ["digital_b1", "digital_b1() -> bool", "デジタル B1", "digital_b1()"],
  ["digital_b2", "digital_b2() -> bool", "デジタル B2", "digital_b2()"],
  ["motion_sensor", "motion_sensor() -> bool", "内蔵モーション(人感)センサー", "motion_sensor()"],

  // --- 出力 ---
  ["run_digital_set", "run_digital_set(target, level)", "デジタル出力 ON/OFF (level: bool)", "run_digital_set("],
  ["run_pwm_set", "run_pwm_set(target, level)", "PWM出力 (level: 0-100)", "run_pwm_set("],
  ["run_servo_turn", "run_servo_turn(target, speed, angle)", "サーボを回す (speed:0-100, angle:-90..90)", "run_servo_turn("],
  ["run_pin_bias_set", "run_pin_bias_set(pin, bias)", "入力ピンのプルアップ設定", "run_pin_bias_set("],

  // --- カラーLED (NeoPixel) ---
  ["run_color_led_set_strip", "run_color_led_set_strip(target, length)", "LEDテープの個数を設定", "run_color_led_set_strip("],
  ["run_color_led_set_color", "run_color_led_set_color(target, position, color)", "1つのLEDに色 (position:1始まり)", "run_color_led_set_color("],
  ["run_color_led_fill_color", "run_color_led_fill_color(target, color)", "全LEDを同色に", "run_color_led_fill_color("],
  ["run_color_led_shift_color", "run_color_led_shift_color(target, n, loop)", "色をずらす", "run_color_led_shift_color("],
  ["run_color_led_show", "run_color_led_show()", "LEDの表示を反映", "run_color_led_show()"],
  ["run_color_led_clear", "run_color_led_clear(target)", "LEDを消灯", "run_color_led_clear("],

  // --- IRリモコン / I2C ---
  ["run_ir_remote_send", "run_ir_remote_send(target, command)", "赤外線リモコン送信 (command:0-9)", "run_ir_remote_send("],
  ["run_i2c_write", "run_i2c_write(address, register, data)", "I2C書き込み", "run_i2c_write("],
  ["fetch_i2c_read", "fetch_i2c_read(address, register, length)", "I2C読み出し", "fetch_i2c_read("],

  // --- 通信（共有サーバー）---
  ["run_share_connect", "run_share_connect(group_id)", "通信: グループに接続（group_id は合言葉）", "run_share_connect("],
  ["run_share_send", "run_share_send(label, data)", "通信: ラベルを付けて値を送る", "run_share_send("],
  ["shared_data", "shared_data(label) -> str", "通信: 受け取った値を取り出す", "shared_data("],
  ["is_share_server_connected", "is_share_server_connected", "通信: 共有サーバーに接続中か", "is_share_server_connected"],

  // --- バージョン ---
  ["fetch_version", "fetch_version() -> dict", "ボードのバージョン", "fetch_version()"],
  ["fetch_uid", "fetch_uid() -> str", "ボードのUID", "fetch_uid()"],
].map(([name, sig, doc, insert]) => ({ name, sig, doc, insert }));

const AKADAKO_STATICS = [
  ["connect", "connect(name_filter=None) -> AkaDako", "ボードに接続して返す", "connect()"],
  ["ServoWrite", "ServoWrite", "サーボ出力先 (A1, A2, ...)", "ServoWrite."],
  ["PwmWrite", "PwmWrite", "PWM出力先", "PwmWrite."],
  ["DigitalWrite", "DigitalWrite", "デジタル出力先", "DigitalWrite."],
  ["DigitalRead", "DigitalRead", "デジタル入力先", "DigitalRead."],
  ["AnalogRead", "AnalogRead", "アナログ入力先", "AnalogRead."],
  ["ColorLed", "ColorLed", "カラーLED接続先", "ColorLed."],
  ["IrRemoteWrite", "IrRemoteWrite", "赤外線出力先", "IrRemoteWrite."],
  ["PinBias", "PinBias", "プルアップ設定 (NONE / PULL_UP)", "PinBias."],
  ["Color", "Color", "色定数 (RED, GREEN, ...)", "Color."],
  ["Rainbow", "Rainbow", "レインボー", "Rainbow."],
].map(([name, sig, doc, insert]) => ({ name, sig, doc, insert }));

window.AKADAKO_BOARD_API = BOARD_API;
window.AKADAKO_STATICS = AKADAKO_STATICS;

// センサーモニタの表示名（メソッド名 -> 日本語ラベル）。
// 未登録の名前（monitor("...") で付けた任意名など）はそのまま表示される。
window.AKADAKO_LABELS = {
  analog_brightness: "明るさ(内蔵)",
  motion_sensor: "人感センサー",
  analog_a1: "アナログ A1",
  analog_a2: "アナログ A2",
  analog_b1: "アナログ B1",
  analog_b2: "アナログ B2",
  digital_a1: "デジタル A1",
  digital_a2: "デジタル A2",
  digital_b1: "デジタル B1",
  digital_b2: "デジタル B2",
  fetch_temperature: "温度(℃)",
  fetch_humidity: "湿度(%)",
  fetch_pressure: "気圧(hPa)",
  fetch_brightness: "明るさ(I2C・lx)",
  fetch_optical_distance: "距離(cm)",
  fetch_water_temperature_a: "水温A(℃)",
  fetch_acceleration_x: "加速度 X",
  fetch_acceleration_y: "加速度 Y",
  fetch_acceleration_z: "加速度 Z",
  fetch_acceleration_magnitude: "加速度の大きさ",
  fetch_pitch: "ピッチ(度)",
  fetch_roll: "ロール(度)",
  fetch_version: "バージョン",
  fetch_uid: "UID",
};
