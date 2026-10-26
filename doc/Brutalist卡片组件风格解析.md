---
AIGC:
    Label: "1"
    ContentProducer: 001191440300708461136T1XGW3
    ProduceID: 619a68374c198f2d1dccd9a31deded68_731875d0aff811f18039525400461939
    ReservedCode1: hgqmzskgUBgcrcw4v7G2MgK7Afmviv67R9rSIfzV5FxU57w1TgUNxqdiXqQBaIFPjnUoYT4rRN53qASAR/GgxL66dgNXUQ84mp/YAkxz6BBEGFLzOmJe4FeBoCZPmKtwEFcTaSHigoNBvdl8XlcvbBvCTFy+HBpsC3Jc5Wctys/v9lLuKbPiA87JmuU=
    ContentPropagator: 001191440300708461136T1XGW3
    PropagateID: 619a68374c198f2d1dccd9a31deded68_731875d0aff811f18039525400461939
    ReservedCode2: hgqmzskgUBgcrcw4v7G2MgK7Afmviv67R9rSIfzV5FxU57w1TgUNxqdiXqQBaIFPjnUoYT4rRN53qASAR/GgxL66dgNXUQ84mp/YAkxz6BBEGFLzOmJe4FeBoCZPmKtwEFcTaSHigoNBvdl8XlcvbBvCTFy+HBpsC3Jc5Wctys/v9lLuKbPiA87JmuU=
---

# Brutalist 卡片组件风格解析

> 本文档基于样例组件 `temp/brutalist_card_sample.vue`（告警卡片）提取其前端风格，整理为可落地复用的规范形式，供后续在项目中复刻或评估参考。

---

## 1. 风格概述

一句话定性：**新粗野主义（Neo-Brutalism）** —— 以 4px 黑色硬边框、无模糊硬阴影、纯黑/纯白底色、锐利直角、大面积撞色完成为特征，视觉硬朗、直白、高对比，具有「印刷海报 / 贴纸」般的压迫感与存在感。

核心气质关键词：**硬、直、重、撞**（硬边框、直角、重阴影、强撞色）。

---

## 2. 设计令牌 / 数值提取

### 2.1 配色方案

| 令牌 | 色值 | 用途 |
|---|---|---|
| `--clr-bg` | `#FFFFFF` | 卡片主背景 |
| `--clr-ink` | `#000000` | 文字主色、边框、图标块背景 |
| `--clr-mark` | `#296FBB` | "Mark as Read" 按钮 hover 底/边 |
| `--clr-mark-shadow` | `#004280` | "Mark as Read" hover 阴影（加深蓝） |
| `--clr-danger` | `#FF0000` | "Okay" 按钮 hover 底/边（危险强调） |
| `--clr-danger-shadow` | `#800000` | "Okay" hover 阴影（加深红） |
| `--clr-sheen` | `rgba(255,255,255,0.3)` | 按钮扫光渐变高光 |
| 按钮默认 | 底 `#fff` / 文字 `#000` | 普通态 |
| 反色按钮（--read） | 底 `#000` / 文字 `#fff` | 反转强调 |

> 规则：**阴影一律用「比 hover 色更深一档」的同色系**（蓝 296FBB→004280、红 FF0000→800000），保证硬阴影层次。

### 2.2 字体

| 属性 | 值 |
|---|---|
| 字体族 | `"Arial", sans-serif`（无衬线，非手写/衬线） |
| 标题（alert） | 字重 `900`、字号 `1.5rem`、`text-transform: uppercase` |
| 正文（message） | 字重 `600`、字号 `0.9rem`、行高 `1.4` |
| 按钮 | 字重 `700`、字号 `1rem`、`text-transform: uppercase` |
| 通用 | 全组件大写化、粗字重，营造通告/标语感 |

### 2.3 尺寸与布局

| 令牌 | 值 |
|---|---|
| 卡片宽度 | `320px`（固定宽） |
| 卡片内边距 | `1.5rem` |
| 卡片边框 | `4px solid #000` |
| 标题区下边框 | `2px solid #000`，底部内边距 `1rem` |
| 正文下边框 | `2px solid #000`，底部内边距 `1rem` |
| 图标块 | 黑底 `#000`、内边距 `0.5rem` |
| 图标 SVG | `1.5rem × 1.5rem`、`fill: #fff` |
| 按钮边框 | `3px solid #000`、内边距 `0.75rem`、`width: 100%` |
| 按钮间距 | 上下 `1rem`（margin-bottom） |

### 2.4 阴影（硬阴影，无模糊）

| 场景 | 阴影值 |
|---|---|
| 卡片 | `10px 10px 0 #000` |
| 按钮默认 | `5px 5px 0 #000` |
| 按钮 hover | `7px 7px 0 #000`（加深）/ 蓝 `7px 7px 0 #004280` / 红 `7px 7px 0 #800000` |
| 按钮 active | `box-shadow: none`（按下贴平） |

> 关键：所有阴影 `blur = 0`，纯偏移硬投影，无柔化。

### 2.5 圆角

| 元素 | border-radius |
|---|---|
| 全部元素 | **`0`（锐利直角）** |

> 与项目现有胶囊按钮（500px 圆角）形成最大反差点。

---

## 3. 组件结构规范（BEM 命名）

采用 `block__element--modifier` 体系：

```
brutalist-card                    block
├── brutalist-card__header        element（标题行：图标 + alert 标题）
│   ├── brutalist-card__icon      element（黑底图标块）
│   └── brutalist-card__alert     element（大号告警标题）
├── brutalist-card__message       element（正文）
└── brutalist-card__actions       element（操作区）
    ├── brutalist-card__button                 element（基础按钮）
    │     ├── brutalist-card__button--mark     modifier（默认白底：Mark as Read）
    │     └── brutalist-card__button--read     modifier（反色黑底：Okay）
```

四段式布局序列：**header（图标+标题）→ message（正文）→ actions（按钮组）**。

样式范围：全部样式 `scoped`，仅作用于组件内部（.vue SFC），不污染全局。

---

## 4. 交互与动画

| 交互 | 效果 | 实现 |
|---|---|---|
| 按钮 hover | 向左上平移 `translate(-2px,-2px)`，阴影叠加到 `7px 7px 0 <色>`，产生"被抬起"感 | `.brutalist-card__button:hover` |
| 扫光 | `::before` 渐变条从 `left: -100%` 扫到 `100%`（120deg 透明→白高光→透明），`0.6s` | `:hover::before { left: 100% }` |
| active 按压 | 位移 `translate(5px,5px)` + 阴影清除，模拟"按下去"的物理反馈 | `.brutalist-card__button:active` |
| 色块 hover | `--mark` 变蓝底白字；`--read` 变红底白字（border-color 同步变色） | modifier 专属规则 |
| 过渡 | `transition: all 0.2s ease`（扫光 0.6s） | 全局过渡 |

> 按压位移 5px > hover 位移 2px，通过位移/阴影差强化按压层级。

---

## 5. 与项目现有暖米黄设计语言（PaperPotato）的差异与冲突点

**说明：以下仅为客观差异提示，不构成推荐结论。**

| 维度 | Brutalist 卡片 | 项目现有（PaperPotato） | 冲突点 |
|---|---|---|---|
| 主背景 | 纯白 `#FFFFFF` | 暖米黄 `#E9DFC3` / cream `#F0E6C8` | 基底色冷暖、明度差异大 |
| 主色 | 纯黑 `#000000` | 深墨绿点缀 | 强调方向不同 |
| 圆角 | `0` 锐利直角 | 按钮 `500px` 胶囊、常规 8px | **直接冲突**，同一页面会出现风格割裂 |
| 边框 | `4px` 粗黑硬边 | 浅色细边框 `#e9e1cc` | 视觉重量相反 |
| 阴影 | 无模糊硬阴影（blur 0） | 柔和分层（Tailwind shadow 体系） | 深度表达方式相反 |
| 危险色 | `#FF0000`（高饱和纯红） | `#c0564f`（低饱和陶土红） | 饱和冲突，红色告警会显得"跳出" |
| 文字 | Arial、全大写、900 重字重 | 衬线体、深浅灰 `#333/#888` | 字重/大小写习惯冲突 |
| 质感 | 印刷贴纸、贴纸化 | 纸感柔和、低对比 | 整体气质对立 |

**关键结论性提示（客观）**：两套语言在「圆角、边框粗细、阴影软硬、主背景冷暖」四个维度实质对立；若在暖米黄界面中引入 Brutalist 卡片，需显式定义其边界（如仅用于强告警/危险操作场景）或提供 token 化配色映射，否则破坏整体一致性。

---

## 6. 可复用复刻要点（CSS 核心片段清单）

以下为在 Vue 中直接复刻本例所需的关键规则清单：

```css
/* 1. 卡片：固定宽 + 粗黑边 + 无模糊硬阴影 + 直角 */
.brutalist-card {
  width: 320px;
  border: 4px solid #000;
  background-color: #fff;
  padding: 1.5rem;
  box-shadow: 10px 10px 0 #000;   /* blur=0 硬阴影 */
  border-radius: 0;                /* 锐利直角 */
  font-family: "Arial", sans-serif;
}

/* 2. 标题区：下边框分隔 + 大号大写粗标题 */
.brutalist-card__header { border-bottom: 2px solid #000; padding-bottom: 1rem; }
.brutalist-card__alert  { font-weight: 900; font-size: 1.5rem; text-transform: uppercase; }

/* 3. 图标块：黑底白图标，1.5rem 方形 */
.brutalist-card__icon { background-color: #000; padding: 0.5rem; }
.brutalist-card__icon svg { width: 1.5rem; height: 1.5rem; fill: #fff; }

/* 4. 按钮基座：3px 边 + 硬阴影 + 相对定位（供扫光定位） */
.brutalist-card__button {
  border: 3px solid #000;
  background-color: #fff;
  color: #000;
  box-shadow: 5px 5px 0 #000;
  padding: 0.75rem;
  position: relative;
  overflow: hidden;
  transition: all 0.2s ease;
}

/* 5. 扫光：::before 从 -100% 扫到 100%（120deg 渐变） */
.brutalist-card__button::before {
  content: "";
  position: absolute; top: 0; left: -100%;
  width: 100%; height: 100%;
  background: linear-gradient(120deg, transparent, rgba(255,255,255,0.3), transparent);
  transition: all 0.6s;
}
.brutalist-card__button:hover::before { left: 100%; }

/* 6. hover：上移 2px + 阴影加深 */
.brutalist-card__button:hover {
  transform: translate(-2px, -2px);
  box-shadow: 7px 7px 0 #000;
}

/* 7. active 按压：位移 5px + 去阴影 */
.brutalist-card__button:active {
  transform: translate(5px, 5px);
  box-shadow: none;
}

/* 8. modifier 色块 hover：深色 hover 阴影 = hover 色加深一档 */
.brutalist-card__button--mark:hover { background-color:#296fbb; border-color:#296fbb; color:#fff; box-shadow:7px 7px 0 #004280; }
.brutalist-card__button--read:hover { background-color:#ff0000; border-color:#ff0000; color:#fff; box-shadow:7px 7px 0 #800000; }
```

### 复刻检查清单（落地时逐项核验）

- [ ] 直角（border-radius 全部为 0）
- [ ] 硬阴影（blur=0，仅偏移）
- [ ] 边框层级统一（卡片 4px / 按钮 3px / 内部分隔 2px）
- [ ] 全大写 + 粗字重（700/900）
- [ ] hover 位移 -2px 与阴影叠加、active 位移 +5px 去阴影
- [ ] 扫光渐变残留 transform/overflow 兼容（需 `overflow:hidden`）
- [ ] 反色与危险色按钮的 hover 阴影使用「同色系加深一档」

---

*文档生成日期：2026-09-14*
*（内容由AI生成，仅供参考）*
