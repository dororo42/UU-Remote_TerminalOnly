# 终端链路 trace 事件参考

两个诊断文件都是**追加式**、按进程分条目,内容刻意做到 phase-only(无
token、端口、句柄值以外的句柄、终端字节)。排障时把三个文件的时间线对齐
(UTC)即可还原一次控制端尝试的完整生命周期。

## 1. `bin/uu-conpty.trace`(shim,宿主 = conpty_bridge 进程)

| 事件 | 含义 | 期望频次 |
|---|---|---|
| `create_flags <flags>` | CreatePseudoConsole 被调用;flags=7 含 INHERIT_CURSOR(shim 会丢弃该标志) | 每会话 1 次 |
| `direct_session <cols<<16\|rows>` | 已连上 PTY broker(ATTACH 角色);值为控制台尺寸 | 每会话 1 次 |
| `resize <cols<<16\|rows>` | 控制端调整窗口尺寸 | 用户拖拽时 |
| `wine_fallback <hresult>` | broker 不可达,回退 Wine 原生 pseudoconsole(终端将不可用) | 正常 **0**;>0 = 桥/网络问题 |
| `broker_closed <err>` | broker 侧关闭连接;0=优雅 FIN,10054=RST | 会话结束时 1 次;**中途出现 = 会话被拆** |
| `close <owned>` | conpty_bridge 调 ClosePseudoConsole;owned=1 表示是我们管理的会话 | 每会话 1 次 |
| `input_ended <err>` | 输入管道结束;109=broken pipe(通常为关闭级联的下游) | 每会话 1 次 |

已知良性瞬态:PC 控制端窗口初始化会发一帧 **120x9001** 的 resize(已容忍,
见 `terminal resize frame ignored`)。

## 2. `system32/WindowsPowerShell/v1.0/uu-terminal-proxy.trace`(mux 代理)

| 事件 | 含义 | 期望频次 |
|---|---|---|
| `mux_script[N] <text>` | 控制端发来的启动脚本全文(分块,仅含 uuyc-mux/chcp) | 每会话 1 组 |
| `mux_step exit=<code>` | 脚本中一条 uuyc-mux 语句执行完成 | new 一条、set-option/rename 各一条、attach 常驻 |
| `anchor_held` | 面板 shell 以 ANCHOR 角色锚定持久会话 | 每会话 1 次;**之后消失且无 anchor_released = 进程被硬杀**(PC 树清理) |
| `anchor_hold_timeout` | 桥 5s 未关锚连接(桥停滞) | 正常 **0** |
| `anchor_released` | 桥端正常关闭锚连接(会话结束) | 会话结束时 1 次 |
| `anchor_busy` | broker 回 BUSY(0x07):会话/锚定名额已满 | 正常 **0** |
| `unsupported_command` | UU 的 visible-attach helper 被设计内拒绝,UU 转向 cmd 候选 | PC 每次尝试 1 次(设计内) |
| `script_done_holding_session` | 启动脚本执行完毕,代理持有锚直到会话结束 | PC 每次尝试 1 次 |
| `send_stalled_terminating` | viewer 方向 5s 不可写,连接已坏,进程退出交由 UU 重拉 | 正常 **0** |
| `mux_script_truncated` | 启动脚本超过 8 块(≈3KB)——重新审查 UU 在要求什么 | 正常 **0** |

## 3. `~/.local/state/uu-remote-bridge/terminal-bridge.log`(broker)

| 行 | 含义 |
|---|---|
| `terminal bridge ready on loopback port <N>` | 桥就绪(每次桥重启新端口) |
| `terminal session opened pid=<shell> size=<w>x<h> name=<N>` | PTY 会话建立 |
| `terminal session attached name=<N>` | 一个 viewer(通常 shim)接入 I/O |
| `terminal anchor rejected: anchors=%d limit=%d` + BUSY | 锚定名额满(0x07 已回) |
| `terminal join_after_race attempt=<n>` | 会话建立竞态,退避后重试 |
| `terminal join raced three times; refusing with BUSY` | 三次竞态耗尽(0x07 已回) |
| `terminal resize frame ignored` | 越界 resize 被忽略(见 §1 良性瞬态) |
| `terminal session anchor closed name=<N>` | 锚连接关闭;有活跃 viewer 时会话继续(0c55775 语义) |
| `terminal session closed name=<N>` | 会话结束 |
| `terminal handshake rejected on a connection` / `terminal connection rejected: peer check` | 握手/对端校验失败(应仅出现在攻击或异常客户端下) |
| `taking over a stale ready file (port <N> is dead)` | 接管上次崩溃残留的 ready 文件 |

## 判读速查

- `broker_closed 10054` 先于 `anchor closed` → 面板/锚先死(关注 mux trace)。
- `broker_closed` 之前有 `terminal resize frame ignored` → 控制端发了异常
  尺寸,已容忍,后续帧正常即可忽略。
- `anchor_hold_timeout` → 桥进程停滞(检查桥是否被 SIGSTOP/宿主挂起)。
