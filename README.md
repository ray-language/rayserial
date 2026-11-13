# rayserial

Un terminal serie para placas tipo Arduino/ESP32 (picocom/minicom con una capa de guiones
*expect*), escrito en raylang. Es una app de *dogfooding*: existe para ejercitar la parte de
raylang que ninguna otra app tocaba — **`std/ffi` y `extern` sobre libc** (termios,
`open`/`read`/`write`/`close`, `posix_openpt`, `poll` como extern `blocking`,
`opendir`/`readdir`), **punteros `ptr`** (las `dirent` se copian de memoria C con `memcpy`),
**`ffi.errno`**, **`ffi.buffer`** como parámetro de salida, **`std/term`** (modo crudo del
terminal local), `std/io` a nivel de octetos, `signals()` + `try_recv`, y `bytes` por todas
partes — y apuntar lo que se rompe o falta.

```
$ rayserial fake-board --sensor-ms 2000 &
PORT /dev/ttys012
$ rayserial open /dev/ttys012 --crlf
*** /dev/ttys012: 115200 8N1 flow=none (speed via termios) DTR=on RTS=on
*** Ctrl-A x quits, Ctrl-A ? lists the keys
led on
LED ON
sensor n=3 temp=23.3 hum=43
                                   (Ctrl-A p: pulso de DTR = reset del Arduino)
*** DTR pulsed 100 ms (B0 hangup)

rayserial fake board 1.0 (Arduino-like sketch)
type 'help' for commands
ready
```

**No se probó con una placa real**: en esta máquina no hay ninguna conectada. Todo se ha
probado contra pseudoterminales (ver [Cómo se probó](#cómo-se-probó)).

## Uso

```
rayserial list [--all]
rayserial open PORT [LÍNEA] [--echo] [--crlf] [--hex] [--quiet] [--for SEGS]
rayserial send PORT FICHERO|- [LÍNEA] [--chunk N] [--delay MS] [--rx FICHERO|-] [--rx-idle MS]
rayserial capture PORT -o FICHERO|- [LÍNEA] [--for SEGS] [--bytes N] [--idle MS]
                  [--timestamps] [--send TEXTO]
rayserial expect PORT GUION [LÍNEA] [--timeout SEGS] [--quiet]
rayserial fake-board [--mode sketch|echo|sink|source] [--for SEGS] [--sensor-ms N]
                  [--rate OCTETOS_POR_S] [--bytes N] [--exit-idle MS] [--link RUTA]

LÍNEA:  -b/--baud N (115200)  --data 5|6|7|8  --parity none|even|odd  --stop 1|2
        --flow none|rtscts|xonxoff
```

| Orden | Qué hace |
|---|---|
| `list` | los dispositivos serie de `/dev` (`cu.*`, `tty.usb*`, `ttyUSB*`, `ttyACM*`, `ttyAMA*`, `rfcomm*`), leídos con `opendir`/`readdir` por FFI y clasificados (`usb`, `bluetooth`, `system`, `other`); los de Bluetooth y sistema (`cu.debug-console`…) solo con `--all`. Solo lista: nunca abre ninguno |
| `open` | terminal interactivo: el terminal local en modo crudo (`std/term`), un bucle `poll(2)` sobre el teclado y el puerto. Escape **Ctrl-A** y: `x` salir · `b` cambiar baudios (pide el número) · `h` vista hexadecimal · `t` conmutar DTR · `r` conmutar RTS · `p` pulso de DTR de 100 ms (el reset de un Arduino) · `k` *break* · `s` estado (leído con `tcgetattr`) · `?` ayuda · Ctrl-A Ctrl-A manda un Ctrl-A. `--crlf`: Intro sale como CR LF y un LF suelto se muestra como CR LF; `--echo`: eco local. Con la entrada en un tubo (`printf 'ping\r' \| rayserial open P --for 2`) manda lo leído y sigue mostrando hasta `--for` |
| `send` | manda un fichero (o la entrada estándar, en flujo) por trozos de `--chunk` octetos con `--delay` ms entre ellos; mientras tanto **lee** lo que llega (a `--rx`, o lo descarta), porque una placa a la que nadie lee acaba bloqueada y deja de leer también. Al final `tcdrain` y, con `--rx-idle`, sigue leyendo hasta ese silencio |
| `capture` | guarda lo que llega en un fichero (o `-o -`); para con `--for`, `--bytes`, `--idle` (tras el primer octeto), SIGINT/SIGTERM o si el puerto desaparece. `--timestamps` pone `[2026-11-13T17:05:01.123Z] ` delante de cada línea; `--send` manda un texto (con escapes `\r \n \xNN`) nada más abrir |
| `expect` | ejecuta un guion (abajo); sale 0 si todo casa y 1 con `line N: timeout (…) waiting for "…"; last received: "…"` si no |
| `fake-board` | la placa de mentira: abre un par pty, escribe `PORT /dev/ttysNNN` y se comporta como un *sketch* de Arduino (ver abajo) |

### Guiones de `expect`

```
# comentario
timeout 5s            espera por defecto de los expect siguientes
reset [100ms]         pulso de DTR (autoreset de Arduino)
expect "ready"        espera a que lo recibido contenga el texto
expect /t=\d+/        ... o case con una regex (std/regex)
send "led on\r\n"     manda octetos (escapes \r \n \t \0 \\ \" \xNN)
sendline led on       manda el resto de la línea + CR LF
sleep 250ms           espera (ms, s, o un número = ms), leyendo lo que llegue
baud 9600             cambia la velocidad del puerto
flush                 olvida lo recibido hasta ahora
log texto             nota por stderr
```

Lo recibido se busca como octetos (un `expect` literal casa con `bytes.index_of`) y, para las
regex, como texto latin-1 (un carácter por octeto), así que un volcado binario no rompe nada.
Cada `expect` consume lo recibido hasta el final de su coincidencia.

### La placa de mentira

`rayserial fake-board` abre un par con `posix_openpt` y se queda con un descriptor del esclavo
abierto y en crudo (así lo que escribe nunca vuelve como eco de la disciplina de línea, y el
maestro no da EIO entre clientes). Modos:

- `sketch` (por defecto): *banner* al arrancar y tras cada reset por DTR, eco de lo que recibe
  (CR → CR LF, retroceso), y responde `led on`, `led off`, `led?`, `ping`, `baud?` (la velocidad
  que el cliente puso, leída con `tcgetattr` del maestro), `boots?`, `dump N` (N octetos 0..255
  entre `BEGIN N` y `END`) y `help`; cada `--sensor-ms` una línea `sensor n=… temp=… hum=…`
  determinista.
- `echo`: devuelve cada octeto tal cual.
- `sink`: lee y cuenta, opcionalmente a `--rate` octetos/s (un par lento); al terminar escribe
  `RX <n> bytes sha256 <hex>`.
- `source`: al primer octeto recibido escribe `--bytes` octetos de un flujo pseudoaleatorio fijo;
  `TX <n> bytes sha256 <hex>`.

**El reset por DTR.** Un pty no tiene líneas de módem (`TIOCMGET`/`TIOCMBIS` dan `ENOTTY`), pero
comparte un único termios entre maestro y esclavo. rayserial baja DTR como manda POSIX cuando
no hay ioctl de módem — velocidad de salida 0 (`B0`, *hangup*) — y la placa lo ve consultando
el termios del maestro cada ≤ 20 ms: velocidad 0 = DTR bajo; al volver la velocidad, «reinicia»
(LED apagado, contador de arranques, *banner*). Con un adaptador USB real rayserial usa
`TIOCMBIC`/`TIOCMBIS`.

## Cómo está hecho

| Módulo | Qué |
|---|---|
| `sys.ray` | **todos los `extern`** (libc) y las constantes de plataforma (macOS/Linux): `open` `read` `write` `close` `isatty` `strerror` `tcgetattr` `tcsetattr` `tcflush` `tcdrain` `tcsendbreak` `posix_openpt` `grantpt` `unlockpt` `ptsname` `opendir` `readdir` `closedir` `memcpy` `__ioctl`/`ioctl`, y `poll` en un bloque `extern "c" blocking`. `poll_fds` arma el array `struct pollfd` en un `bytes` que C rellena; `read_chunk`/`write_all` sobre descriptores no bloqueantes; mensajes con `strerror` + `ffi.errno()` leído justo tras la llamada |
| `termios.ray` | `struct termios` **a mano**: en macOS 72 octetos (`tcflag_t`/`speed_t` de 8, `c_cc[20]` en 32, velocidades en 56/64), en Linux 60 (de 4, `c_line` en 16, `c_cc[32]` en 17, velocidades en 52/56); constantes de bits por plataforma; tabla de códigos `B*` de Linux; `make_raw` (cfmakeraw escrito), `configure` (datos/paridad/parada/flujo, `CREAD\|CLOCAL`) y `describe_config` (la inversa), todo puro |
| `port.ray` | abrir (`O_RDWR\|O_NOCTTY\|O_NONBLOCK\|O_CLOEXEC`, comprobar que es un tty, guardar el termios previo), configurar, **velocidades no estándar** (en macOS: `tcsetattr` a 9600 y luego `IOSSIOSPEED` con `__ioctl`; si el driver lo rechaza — un pty da `ENOTTY` — la velocidad va directa en termios, que el pty acepta), DTR/RTS (`TIOCMBIS`/`TIOCMBIC`, o `B0` si no hay líneas de módem), *break*, `tcdrain`, y cerrar restaurando el termios original |
| `pty.ray` | el par pty de la placa |
| `ports.ray` | `list`: `opendir`/`readdir` devuelven `Option<ptr>`; la `dirent` se copia a un `ffi.buffer` con `memcpy(dst: bytes, src: ptr, n)` (cabecera de 24 octetos, luego `d_reclen`) y se decodifica según la plataforma |
| `keys.ray` | la máquina de estados (pura) del teclado: texto, Ctrl-A + orden, el *prompt* de baudios con retroceso y Esc |
| `view.ray` | vista hexadecimal que conserva la columna entre ráfagas, LF → CR LF, latin-1, escapado para mensajes |
| `session.ray` | `open`: `term.raw` envuelve el bucle `poll` sobre fd 0 y el puerto |
| `transfer.ray` | `send` y `capture`: bucles `poll` con `POLLIN\|POLLOUT`, `signals()` + `try_recv`, marcas de tiempo, ficheros con `fs.open`/`fs.write_bytes` |
| `script.ray`, `runner.ray` | el lenguaje de los guiones (parser puro) y su ejecución contra un puerto |
| `board.ray` | la placa de mentira (el *sketch* es puro y está probado aparte); `sha256` incremental de `std/crypto` |
| `main.ray` | la CLI |

## Cómo se probó

**Oráculo independiente: la biblioteca estándar de Python 3** (`pty`, `termios`, `os`,
`select`; `/opt/homebrew/bin/python3`, sin instalar nada). Un pty comparte un solo termios
entre maestro y esclavo, así que `termios.tcgetattr` sobre el **maestro de un pty que abre
Python** lee exactamente lo que rayserial aplicó al esclavo; y Python escribe y lee el maestro
como par independiente.

| Suite | Qué | VM | nativo |
|---|---|---:|---:|
| `make unit` / `unit-native` (`ray test`) | lógica pura (termios por plataforma, tablas de baudios, teclas, vistas, guiones, *sketch*) y **pruebas en proceso sobre un pty real**: la configuración llega a la línea (leída del maestro), cerrar restaura, los 256 valores en ambos sentidos, 1 MiB por trozos con sha256, 250000/74880 baudios en un pty (`IOSSIOSPEED` → `ENOTTY` → termios), DTR por `B0`, errores con errno, `readdir` por punteros = `fs.list_dir` (también en `/dev`), un guion `expect` contra un *sketch* en otra fibra, XON/XOFF de la placa pausando nuestra salida | **28/28** | **28/28** |
| `make e2e` / `e2e-native` (`scripts/e2e.py`) | **oráculo termios** para 8 configuraciones (115200 8N1; 9600 7E1; 57600 8O2 rtscts; 19200 5N1 xonxoff; 300 6O1; 250000; 1000000; 74880): `CSIZE`, `PARENB`, `PARODD`, `CSTOPB`, `CRTSCTS`, `CREAD\|CLOCAL`, `IXON/IXOFF`, `ICRNL/INLCR/IGNCR/ISTRIP/BRKINT/PARMRK` apagados, `OPOST` apagado, `ICANON/ECHO/ECHONL/ISIG/IEXTEN` apagados, `VMIN=1 VTIME=0`, `ispeed = ospeed = baudios`; y el termios **restaurado** al salir. Octetos con Python de par: los 256 valores ×4 + 256 KiB aleatorios en `capture` y en `send` (también desde stdin). Contra la placa: guion `expect` completo (reset + *banner*, LED, `ping` partido en dos `send`, cambio de baudios que la placa lee del termios, regex del sensor, segundo reset), guion que falla con su número de línea, guion mal escrito, volcado binario exacto, ida y vuelta de 256 KiB por la placa eco, 1 MiB a la placa sumidero y 1 MiB + 77 de la placa fuente con sha256 iguales a los de `hashlib`, **par lento** (50 kB/s: `send` espera, no pierde nada), `--timestamps`, `open` con la entrada por tubo, y la **sesión interactiva** en un segundo pty que hace de terminal del usuario (terminal local crudo según `tcgetattr`, eco, Ctrl-A h/b/p/r/s/x, terminal restaurado), la sesión que termina si la placa desaparece, la placa contestando a un cliente Python que baja DTR con `B0`, `list --all` = un `glob` de `/dev`, y errores | **46/46** | **46/46** |

Lo que `term.raw` aplica al terminal local, medido con el oráculo: activa `IGNBRK`; apaga
`BRKINT`, `ICRNL`, `IXON`, `OPOST`, `ECHO`, `ICANON`, `ISIG`, `IEXTEN`; `VMIN=1 VTIME=0`.
Al volver a modo canónico el núcleo de macOS añade `PENDIN` (`0x20000000`) a `c_lflag` por su
cuenta: las comparaciones de «restaurado» lo ignoran.

Todos los procesos que lanzan los guiones son hijos suyos (`subprocess.Popen`), terminan solos
(`--for`, `--exit-idle`) y, como mucho, se paran por su propio `Popen`. Nada se mata por nombre
ni por puerto. `screen` no se usó.

## Rendimiento

`make bench` (`scripts/bench.py`): mediana de 3 ejecuciones, MacBook Apple Silicon, raylang
1.27.95; las tasas son las del propio rayserial (de abrir el puerto al último octeto, sin el
arranque), MB = 10⁶ octetos; sha256 o comparación exacta en cada transferencia.

| Transferencia | Tamaño | VM (MB/s) | nativo (MB/s) |
|---|---|---:|---:|
| techo del pty: Python → Python | 4 MiB | 21,2 | 21,2 |
| Python → `capture` | 4 MiB | 18,5 | 16,7 |
| `send` → Python | 4 MiB | 69,9 | 99,9 |
| `send` → placa sumidero (nativa) | 4 MiB | 57,5 | 49,3 |
| placa fuente (nativa) → `capture` | 4 MiB | 14,2 | 13,1 |
| `send --rx` ↔ placa eco (nativa), trozos de 512 | 1 MiB | 8,5 | 7,9 |
| `send` → sumidero, placa en el mismo motor | 4 MiB | 45,6 | 47,1 |

Lectura: **la VM rinde como el nativo**; manda el pty. En macOS la cola de entrada del esclavo
guarda ~1 KiB (un `write` de 1024 octetos al maestro sin nadie leyendo devuelve 412), así que
hacia rayserial no se pasa de ~20 MB/s ni con Python en los dos lados; en el otro sentido la
cola es mayor. El coste de cada llamada FFI tampoco importa a esta escala: medido aparte,
`getpid` cuesta ~510 ns en la VM y ~1 ns en nativo, y un `poll` declarado `blocking` cuesta
~4,2 µs en nativo frente a ~0,6 µs sin `blocking` (el salto al *pool* de bloqueantes). Un
USB-serie real a 115200 baudios mueve 0,0115 MB/s: cualquiera de las dos sobra.

## Estado y limitaciones

- **No se probó con una placa real** ni con un adaptador USB-serie: no hay ninguno en esta
  máquina. Los caminos que solo existen con hardware — `IOSSIOSPEED` aceptado,
  `TIOCMBIS`/`TIOCMBIC` de DTR/RTS, `CRTSCTS` deteniendo de verdad la salida, *break* — están
  escritos pero solo se han visto fallar limpiamente (`ENOTTY`) en un pty. El control de flujo
  que sí se probó es XON/XOFF (lo hace la disciplina de línea, también en un pty).
- Las velocidades no estándar en macOS van por `IOSSIOSPEED`, que **sí se puede expresar**
  con el FFI de raylang… pero no con `ioctl`, que es variádica: rayserial llama a `__ioctl`, el
  envoltorio no variádico de Darwin (ver hallazgo 3).
- **Linux**: las constantes, el `struct termios` de glibc, los códigos `B*` y la `dirent` están,
  pero **sin probar** (no hay Linux a mano). Velocidades sin código `B*` (250000, 74880) dan
  error en Linux: harían falta `termios2`/`BOTHER` (`TCSETS2`), no implementados.
- La placa de mentira no puede ver el DTR que *sube* al abrir el puerto (en un Arduino real
  abrir el puerto ya lo reinicia); solo ve los pulsos por `B0`.
- `open` no traduce mapas de caracteres más allá de `--crlf`, ni registra la sesión a fichero
  (`capture` lo hace sin terminal).

## Hallazgos de dogfood

Con su reproducción mínima en raylang 1.27.95 release; los rodeos están marcados en el código.

1. **[B] `ray fmt` borra en silencio la llamada a método que sigue a un literal interpolado**
   en ciertas posiciones: `let s = "id-${n}".to_upper() + "!";` queda `let s = "id-${n}";`,
   `for b in "${c}".to_bytes()` queda `for b in "${c}"`, y
   `return "BEGIN ${n}\r\n".to_bytes() + bytes_of(body) + b"…";` queda
   `return "BEGIN ${n}\r\n";`. Si el resultado sigue compilando, el programa cambia sin aviso
   (`ID-7!` pasa a `id-7`). Aquí lo cazaron el compilador y una prueba.
2. **[B] VM: `signals()` no entrega nada a un bucle que solo espera en FFI.** Con `try_recv`
   (o `select_timeout(…, 0)`, o `time.sleep(0)`) y un `poll` por FFI como única espera, un
   SIGTERM no llega nunca en la VM; en nativo llega. Rodeo: `time.sleep(1)` cuando `poll`
   vuelve sin nada.
3. **[E/D] El FFI no llama a funciones variádicas, y `ioctl`/`fcntl` lo son.** En arm64 macOS
   el argumento variádico va por la pila: `ioctl(fd, req, buf)` declarado con aridad fija da
   `EFAULT`, y `fcntl(fd, F_SETFL, O_NONBLOCK)` devuelve 0 **sin hacer nada**. La referencia
   (§13) solo pone `printf` de ejemplo. Rodeo: `__ioctl` (símbolo privado de Darwin) y abrir con
   `O_NONBLOCK` para no necesitar `fcntl`.
4. **[E] `ptr` es opaco del todo**: no hay `NULL` (`0 as ptr` no compila; `Option<ptr>` no vale
   como argumento), ni lectura de memoria, ni aritmética, ni conversión a entero. Rodeos:
   declarar el parámetro como `u64` y pasar `0`, y declarar `memcpy(dst: bytes, src: ptr, n)`
   para leer una `dirent`.
5. **[E] No hay termios en std**: `term.raw` solo actúa sobre la entrada estándar y no dice qué
   bits toca; para un puerto serie hay que codificar `struct termios` a mano por plataforma
   (72 octetos con campos de 8 en macOS, 60 con campos de 4 en Linux, constantes distintas,
   códigos `B*`). ~500 líneas de `termios.ray` existen solo por eso.
6. **[D] El coste de una llamada FFI no está documentado**: ~510 ns por `extern` en la VM
   (frente a ~1 ns en nativo) y ~3,6 µs extra por llamada `blocking` en nativo. Con esas cifras
   se decide cuándo merece la pena `blocking` (aquí, en un `poll` de 20 ms, sí; en uno de 0 ms,
   no).

Positivo: el FFI de raylang aguanta una app de sistema entera — 22 funciones de libc, buffers
de salida (`ffi.buffer`) que C rellena (también a través de un `extern blocking` que corre en
otro hilo), `Option<string>`/`Option<ptr>` de vuelta, `errno` fiable en VM y nativo — y **ninguna
prueba se comportó distinto en VM y en nativo**.

## Licencia

Apache-2.0.
