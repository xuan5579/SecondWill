/** 发光 AI 伙伴 —— 执行力仪表盘的情感载体。

设计约束（v3，纯视觉，不参与任何判定）：

- **去贴纸化**：不再有橙色渐变毛皮、腮红、白高光、粗描边。身体是**深青黑半透明**
  的剪影，靠边缘的紫蓝外光晕把形状"打"出来。这样它在深色玻璃卡片上读起来像一团
  有生命的能量，而不是一张贴图。
- **冷青发光眼**：唯一的高饱和点。竖瞳保留 —— 竖瞳是猫最强的识别特征，做成圆的
  立刻变仓鼠。眼神是这只"伙伴"所有的表情来源。
- **六档靠姿态区分，不靠换色**：耳角、眼皮、瞳孔、倾斜、尾摆各档不同；只有
  警觉 / 愧疚两档会微调眼睛的颜色温度（琥珀 / 玫瑰），其余保持冷青，
  避免"六种颜色的卡通动物"那种廉价感。
- **体色不随档位变**，只有光晕色随档位变 —— 同一个角色在不同情绪下的呼吸，
  而不是六个不同的角色。

viewBox 固定 200×200。外光晕用 CSS `filter: drop-shadow` 加在 svg 上（对整块 alpha
生效，头身接缝处不会多出一道发光线），身体内部的明暗全部来自 SVG 渐变。
*/

export type Mood = 'alert' | 'dormant' | 'tired' | 'guilty' | 'great' | 'normal'

/** 六档清单。演示每一档都需要它。 */
export const MOOD_LIST: Mood[] = ['alert', 'dormant', 'tired', 'guilty', 'great', 'normal']

interface Props {
  mood: string
  size?: number
}

/** 每档的姿态参数：全部是"形状"层面的事实，不参与任何业务判定。 */
const POSE: Record<
  Mood,
  {
    /** 耳朵外旋角度：正值向外张（警觉），负值向后压（心虚 / 休眠） */
    ear: number
    /** 眼皮下压比例，0 = 全睁，1 = 全闭 */
    lid: number
    /** 瞳孔缩放，>1 放大（害怕 / 兴奋），<1 收缩 */
    pupil: number
    /** 头部倾斜（度） */
    tilt: number
  }
> = {
  normal: { ear: 0, lid: 0, pupil: 1, tilt: 0 },
  great: { ear: 3, lid: 0, pupil: 1.14, tilt: -2 },
  alert: { ear: 9, lid: -0.08, pupil: 0.84, tilt: 0 },
  tired: { ear: -6, lid: 0.55, pupil: 1, tilt: 3 },
  dormant: { ear: -11, lid: 1, pupil: 1, tilt: 6 },
  guilty: { ear: -8, lid: 0.26, pupil: 1.06, tilt: 4 },
}

const EYE_Y = 92
const LX = 78
const RX = 122
const EYE_W = 13
const EYE_H = 10.5

/** 杏仁眼：两端收尖。圆眼会立刻变成"卡通黑猫贴纸"，收尖才有"程式化的锐利"。 */
function eyePath(cx: number) {
  return `M${cx - EYE_W} ${EYE_Y} C${cx - EYE_W * 0.45} ${EYE_Y - EYE_H} ${cx + EYE_W * 0.45} ${EYE_Y - EYE_H} ${cx + EYE_W} ${EYE_Y} C${cx + EYE_W * 0.45} ${EYE_Y + EYE_H} ${cx - EYE_W * 0.45} ${EYE_Y + EYE_H} ${cx - EYE_W} ${EYE_Y} Z`
}

/** 闭眼 / 笑眼的弧线（不用眼白，保持"能量体"的抽象度） */
function arcEye(cx: number, up: boolean) {
  const drop = up ? -11 : 8
  return `M${cx - 12} ${EYE_Y + (up ? 2 : 0)} q12 ${drop} 24 0`
}

export default function CatAvatar({ mood, size = 64 }: Props) {
  const key = (mood in POSE ? mood : 'normal') as Mood
  const pose = POSE[key]
  const lid = Math.max(0, Math.min(1, pose.lid))
  const visible = 34 * (1 - lid)

  /* 头：**一条完整轮廓路径**，耳朵直接长在头上。
     用 圆形 + 两个三角拼，永远会被看成"雪人戴纸帽子"；
     猫脸的识别点是腮部的两个鼓包、收窄的下巴、以及耳朵与头的连续过渡。 */
  const earDX = pose.ear * 0.7
  const earDY = -pose.ear * 0.55
  const head = [
    `M ${62 - earDX} ${32 + earDY}`,
    'L 54 64',
    'C 48 86 60 108 74 118',
    'C 84 125 92 129 100 129',
    'C 108 129 116 125 126 118',
    'C 140 108 152 86 146 64',
    `L ${138 + earDX} ${32 + earDY}`,
    'L 118 50',
    'Q 109 54 100 54',
    'Q 91 54 82 50',
    'Z',
  ].join(' ')

  /* 身体：钟形。椭圆的"肩线"是圆的，坐着的猫肩背是一条下滑的弧。
     宽度略大于头 —— 头比肩宽会立刻变成 Q 版幼崽。 */
  const body =
    'M 100 122 C 76 122 58 138 56 162 C 55 184 75 198 100 198 C 125 198 145 184 144 162 C 142 138 124 122 100 122 Z'

  const eyesClosed = key === 'dormant'
  const eyesHappy = key === 'great'

  return (
    <svg
      className={`cat cat-${key}`}
      width={size}
      height={size}
      viewBox="0 0 200 200"
      role="img"
      aria-label={`宠物状态：${mood}`}
    >
      <defs>
        {/* 身体：深青黑的半透明渐变，左上有一点冷光源，往下沉到近黑 */}
        <radialGradient id="ipBody" cx="0.36" cy="0.18" r="0.95">
          <stop offset="0%" stopColor="var(--ip-a)" />
          <stop offset="50%" stopColor="var(--ip-b)" />
          <stop offset="100%" stopColor="var(--ip-c)" />
        </radialGradient>
        {/* 轮廓光：上冷青、下紫，沿身体边缘描一圈细线 ——
            纯黑剪影在深色底上会糊成一团，这一圈线才让轮廓"读得出来" */}
        <linearGradient id="ipRim" x1="0" y1="0" x2="0.35" y2="1">
          <stop offset="0%" stopColor="var(--iris)" stopOpacity="0.75" />
          <stop offset="55%" stopColor="var(--iris)" stopOpacity="0.22" />
          <stop offset="100%" stopColor="var(--fur-mid)" stopOpacity="0.45" />
        </linearGradient>
        {/* 眼睛的发光芯：中心亮到发白，边缘染色 */}
        <radialGradient id="ipEye" cx="0.5" cy="0.42" r="0.62">
          <stop offset="0%" stopColor="#ffffff" stopOpacity="0.98" />
          <stop offset="34%" stopColor="var(--iris)" />
          <stop offset="100%" stopColor="var(--iris)" stopOpacity="0.55" />
        </radialGradient>
        {/* 背后那圈淡紫光环 */}
        <radialGradient id="ipHalo" cx="0.5" cy="0.5" r="0.5">
          <stop offset="0%" stopColor="var(--fur-mid)" stopOpacity="0.17" />
          <stop offset="60%" stopColor="var(--fur-mid)" stopOpacity="0.05" />
          <stop offset="100%" stopColor="var(--fur-mid)" stopOpacity="0" />
        </radialGradient>
        {/* 耳内与胸口的冷光 */}
        <linearGradient id="ipInner" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor="var(--iris)" stopOpacity="0.34" />
          <stop offset="100%" stopColor="var(--iris)" stopOpacity="0" />
        </linearGradient>
        <radialGradient id="ipChest" cx="0.5" cy="0.5" r="0.5">
          <stop offset="0%" stopColor="var(--iris)" stopOpacity="0.13" />
          <stop offset="100%" stopColor="var(--iris)" stopOpacity="0" />
        </radialGradient>
        {/* 眯眼 / 笑眼那条线要"发虚"才像光，不像笔画 */}
        <filter id="ipSoft" x="-60%" y="-60%" width="220%" height="220%">
          <feGaussianBlur stdDeviation="1.5" />
        </filter>
        {/* 眼皮裁切：可见部分保留"下缘"，像真的上眼皮压下来 */}
        <clipPath id="ipLidL">
          <rect x={LX - 18} y={EYE_Y - 18 + (36 - visible)} width={36} height={visible} />
        </clipPath>
        <clipPath id="ipLidR">
          <rect x={RX - 18} y={EYE_Y - 18 + (36 - visible)} width={36} height={visible} />
        </clipPath>
      </defs>

      {/* 光环：淡紫径向光 + 一圈极细的轨道环 */}
      <circle cx={100} cy={104} r={92} fill="url(#ipHalo)" className="ip-halo" />
      <circle
        cx={100}
        cy={104}
        r={72}
        fill="none"
        stroke="var(--fur-mid)"
        strokeWidth={0.9}
        strokeDasharray="2 9"
        opacity={0.45}
        className="ip-orbit"
      />

      {/* 粒子：少量、细腻，绕着头顶缓慢游动 */}
      <g className="ip-dust" fill="var(--iris)">
        <circle cx={40} cy={62} r={1.7} opacity={0.75} />
        <circle cx={162} cy={70} r={1.3} opacity={0.6} />
        <circle cx={54} cy={150} r={1.1} opacity={0.45} />
        <circle cx={150} cy={148} r={1.9} opacity={0.5} />
        <circle cx={100} cy={22} r={1.2} opacity={0.55} />
      </g>

      {/* 尾：在身体后面，以尾根为轴轻摆。描边压在填充之上但更细，
          形成"一亮一暗"的双线，看起来像被光照到的一条轮廓。 */}
      <path
        className="cat-tail"
        d="M 142 168 C 184 176 196 148 176 130"
        fill="none"
        stroke="url(#ipBody)"
        strokeWidth={14}
        strokeLinecap="round"
      />
      <path
        className="cat-tail"
        d="M 142 168 C 184 176 196 148 176 130"
        fill="none"
        stroke="url(#ipRim)"
        strokeWidth={1.4}
        strokeLinecap="round"
        opacity={0.7}
      />

      {/* 身体（坐姿） */}
      <path d={body} fill="url(#ipBody)" />
      <path d={body} fill="none" stroke="url(#ipRim)" strokeWidth={1.3} />
      {/* 胸口的一团冷光，避免下半身糊成一团黑 */}
      <ellipse cx={100} cy={170} rx={34} ry={26} fill="url(#ipChest)" />

      <g style={{ transform: `rotate(${pose.tilt}deg)`, transformOrigin: '100px 140px' }}>
        {/* 头 + 耳朵：同一个形状，所以耳根不会有接缝 */}
        <path d={head} fill="url(#ipBody)" />
        <path d={head} fill="none" stroke="url(#ipRim)" strokeWidth={1.3} strokeLinejoin="round" />
        {/* 耳内：两片向上的冷光，让耳朵不是两个空洞 */}
        <path d={`M ${66 - earDX * 0.5} 60 L ${65 - earDX * 0.8} ${39 + earDY} L ${88} 54 Z`} fill="url(#ipInner)" />
        <path d={`M ${134 + earDX * 0.5} 60 L ${135 + earDX * 0.8} ${39 + earDY} L ${112} 54 Z`} fill="url(#ipInner)" />
        {/* 头顶一道更亮的边光，暗示光源在正上方 */}
        <path
          d="M 82 54 Q 100 48 118 54"
          fill="none"
          stroke="var(--iris)"
          strokeWidth={1.6}
          strokeLinecap="round"
          opacity={0.35}
        />

        {/* ---- 眼睛 ---- */}
        {eyesClosed || eyesHappy ? (
          <g
            stroke="var(--iris)"
            strokeWidth={3.2}
            strokeLinecap="round"
            fill="none"
            opacity={eyesClosed ? 0.5 : 0.95}
            filter="url(#ipSoft)"
          >
            <path d={arcEye(LX, !eyesClosed)} />
            <path d={arcEye(RX, !eyesClosed)} />
          </g>
        ) : (
          <>
            {[LX, RX].map((cx) => (
              <g key={cx} className="cat-eye" clipPath={`url(#ipLid${cx === LX ? 'L' : 'R'})`}>
                {/* 外发光：眼睛周围的冷光雾 */}
                <ellipse cx={cx} cy={EYE_Y} rx={22} ry={19} fill="var(--iris)" opacity={0.13} />
                {/* 眼形：两端收尖的杏仁 */}
                <path d={eyePath(cx)} fill="url(#ipEye)" />
                {/* 竖瞳 —— 猫最强的识别特征，圆瞳会立刻变仓鼠 */}
                <ellipse
                  cx={cx}
                  cy={EYE_Y}
                  rx={3.7 * pose.pupil}
                  ry={8.6 * pose.pupil}
                  fill="#04121a"
                />
                {/* 只留一颗冷色反光，玻璃感来源 */}
                <circle cx={cx - 3.2} cy={EYE_Y - 3.6} r={1.5} fill="#ffffff" opacity={0.92} />
              </g>
            ))}
          </>
        )}

        {/* 鼻 + 一点嘴：极小、极暗，只提供"脸的朝向"，不参与表情 */}
        <path d="M 96.6 110 h 6.8 l -3.4 4.2 z" fill="var(--iris)" opacity={0.42} />
        <path
          d="M 100 114.5 v 3 M 100 117.5 q -5 3 -9 -0.5 M 100 117.5 q 5 3 9 -0.5"
          fill="none"
          stroke="var(--iris)"
          strokeWidth={1.1}
          strokeLinecap="round"
          opacity={0.24}
        />

        {/* 胡须：极细冷光线，只做"这是猫"的暗示 */}
        <g stroke="var(--iris)" strokeWidth={0.9} strokeLinecap="round" opacity={0.3}>
          <path d="M 62 106 l -17 -5 M 62 112 l -18 2" />
          <path d="M 138 106 l 17 -5 M 138 112 l 18 2" />
        </g>

        {/* 警觉：一颗琥珀色告警点，比抖一下更直观 */}
        {key === 'alert' && (
          <circle className="ip-alert-dot" cx={158} cy={44} r={4.5} fill="var(--warn)" />
        )}
        {/* 休眠：两个 z 飘走 */}
        {key === 'dormant' && (
          <g className="cat-zzz" fill="var(--iris)" fontWeight={600} opacity={0.5}>
            <text x={146} y={56} fontSize={17}>z</text>
            <text x={163} y={40} fontSize={11}>z</text>
          </g>
        )}
      </g>
    </svg>
  )
}
