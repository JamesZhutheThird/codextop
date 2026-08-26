# CodexTOP 设置层级

这是当前界面中的完整设置层级，可直接在此调整分类、顺序、名称和选项排版。

## 普通右边栏快捷设置

- F1 / 历史长度（`period`）
  - `5m`
  - `15m`
  - `30m`
  - `1h`
  - `5h`
  - `12h`
  - `1d`
  - `3d`
  - `7d`
  - `30d`
  - `all`
- F2 / 模式切换（`display_scope`）
  - 看板模式（`list`）
  - 专注模式（`focus`）
  - 合并模式（`merged`）
  - 用量模式（`usage`）

## 设置模式

- 设置类型
  - 常规设置（`general`）
    - 更新间隔（`interval`）
      - `5s`
      - `10s`
      - `15s`
      - `30s`
      - `60s`
      - `2m`
      - `5m`
    - 界面语言（`language`）
      - English（`en`）
      - 简体中文（`zh_CN`）
    - 隐藏邮箱（`hide_account_info`）
      - 显示（`false`）
      - 隐藏（`true`）
  - 显示设置（`display`）
    - 额度窗口（`window_scope`）
      - 5h 和 7d（`both`）
      - 仅 5h（`5h`）
      - 仅 7d（`7d`）
    - 看板密度（`dashboard_density`）
      - 默认（`default`）
      - 紧凑（`compact`）
      - 宽松（`relaxed`）
      - 铺满（`fill`）
    - 额度合并（`merged_quota_mode`）
      - 百分比叠加（`percent`）
      - 实际额度叠加（`actual`）
    - 用量范围（`usage_directory_scope`）
      - 当前目录（`current`）
      - 全部目录（`all`）
    - 用量布局（`usage_panel_layout`）
      - 合并（`combined`）
      - 拆分（`split`）
    - 用量坐标轴（`usage_axis_scale`）
      - 线性坐标轴（`default`）
      - 对数坐标轴（`log`）
  - 外观设置（`appearance`）
    - 曲线风格（`curve_mode`）
      - 连续（`connected`）
      - 间断（`points`）
      - 精细（`braille`）
      - 线条（`box`）
      - 柱状（`bar`）
      - 精细柱状（`fine_bar`）
    - 配色方案（`color_scheme`）
      - 经典（`classic`）
      - 明亮（`bright`）
      - 蓝黄（`cividis`）
      - 冷暖（`coolwarm`）
      - 冰与火（`icefire`）
      - 炼狱（`inferno`）
      - 岩浆（`magma`）
      - 等离子（`plasma`）
      - 浅虹（`rainbow`）
      - 涡轮（`turbo`）
      - 翠绿（`viridis`）
  - 软件版本（`software_version`，黄色）
    - Enter：打开软件版本悬浮窗
    - 检查更新（黄色）
    - `<- / 关闭`（Backspace，红色）

## 当前底部固定布局

- 普通模式
  - `F8 / 统计`
  - 空行
  - `F9 / 设置`
  - 空行
  - `F10 / 退出`
  - 空行
  - `@JamesZhutheThird`
  - 版本号
- 设置模式
  - `<- / 返回`（Backspace）
  - 空行
  - `F9 / 退出设置`
  - 空行
  - F10 对应空位
  - 空行
  - `@JamesZhutheThird`
  - 版本号

设置类型之间、设置项之间保留一个空行；具体可选值之间不留空行。
