# 校准和传感器参数调整

- [介绍](#介绍)
- [参数](#参数)
    - [EnterAt](#enterat)
    - [ExitAt](#exitat)
    - [校准模式](#校准模式)
    - [开始比赛 EnterAt/ExitAt 变化规则](#开始比赛-enteratexitat-变化规则)
- [调谐](#调谐)
    - [设置*EnterAt*值](#设置enterat值)
    - [设置*ExitAt*值](#设置exitat值)
    - [调优示例](#调优示例)
    - [替代调整方法](#替代调整方法)
- [提示](#提示)
- [故障排除](#故障排除)
    - [缺少圈数(节点通常*Clear*)](#缺少圈数节点通常clear)
    - [缺少圈数(节点通常*Crossing*)](#缺少圈数节点通常crossing)
    - [被额外多记圈数](#被额外多记圈数)
    - [一次记录多圈](#一次记录多圈)
    - [圈数需要很长时间才能注册](#圈数需要很长时间才能注册)
    - [节点永远不会显示*crossing*](#节点永远不会显示crossing)
    - [节点永远不会显示*clear*](#节点永远不会显示clear)

##介绍
如果您在校准计时器时遇到困难，请确保您已正确构建和放置射设备。

每个节点都会跟踪选定频率上的信号强度 (RSSI)，并使用该相对强度来确定发射器是否靠近定时门。 RotorHazard 计时系统允许您单独校准每个节点，以便您可以补偿系统和环境中的行为和硬件差异。

节点可以是Crossing或Clear。如果节点处于Clear 状态，则系统认为发送器不在定时门附近，因为 RSSI 较低。如果是Crossing，系统认为发送器正在通过定时门，因为 RSSI 很高。一旦穿越完成并且系统返回到Clear 状态，将会记录一圈通过情况。

![Tuning Graph](img/Tuning%20Graph-06.svg)<br />
比赛期间的 RSSI 与此图类似，有许多可见的峰值和谷值。当发射器接近定时门时，信号上升。

##参数
影响Crossing状态的两个参数：EnterAt和ExitAt。

###EnterAt
当 RSSI 升至或高于此水平时，系统将切换到Crossing状态。它由红线表示。
### ExitAt
一旦 RSSI 值低于此水平，系统将切换到Clear状态。它由橙色线表示。

在EnterAt和ExitAt之间，系统将根据其之前的状态保持Crossing或Clear 。

![Sample RSSI Graph](img/Sample%20RSSI%20Graph.svg)

###校准模式

手动校准模式: 将始终使用用户设置的EnterAt和ExitAt值。

自适应校准模式: 使用用户定义的点，除非有已保存的比赛。当保存的比赛存在时，改变预赛将启动对先前比赛数据的搜索，以获得在即将到来的比赛中使用的最佳校准值。这些值将被复制并替换所有节点的当前EnterAt和ExitAt值。如果比赛总监确认传入的圈数或通过Marshal页面重新计算它们，则此模式可以改进校准，因为可以保存更多比赛。

###开始比赛 EnterAt/ExitAt 变化规则

在比赛开始时，可​​能会有许多飞机同时通过起始门，这可能会导致在某些节点上检测到较低的 RSSI 值（这可能会导致错过起始门通过）。为了解决这个问题，可以配置以下设置：

比赛开始 EnterAt/ExitAt 降低量（百分比）：设置所有节点的 EnterAt 和 ExitAt 值将减少的量（百分比）。例如，如果配置了 30（百分比），则 EnterAt 值将降低到接近 ExitAt 值 30% 的值。 （因此，如果 EnterAt=90 且 ExitAt=80，则 EnterAt 值将降低至 87。）ExitAt 值也将降低相同的量。

比赛开始 EnterAt/ExitAt 降低持续时间（秒）：设置 EnterAt 和 ExitAt 值降低的最长时间（以秒为单位）。如果检测到节点的门穿越在此时间之前完成，则该节点的 EnterAt 和 ExitAt 值将被恢复。

建议值为 30（百分比）和 10（秒）。如果这些设置中的任何一个配置为零，则 EnterAt 和 ExitAt 值将不会降低。

注意事项：在Marshal页面上会考虑这些设置，因此如果它们非零，则即使峰值 RSSI 看起来低于显示的 EnterAt 级别，也可能会检测到节点上的第一圈通过。


## 调谐
在调谐之前，启动计时器并让其持续运行几分钟，以使接收模块预热。随着计时器加热，RSSI 值往往会增加几个点。

您可以使用 Marshal 页面直观地调谐值。通过让飞行员在每个频道上进行比赛来运行比赛来收集数据，然后保存它。打开 Marshal 页面并查看比赛数据，调整进入和退出点，直到圈数正确。将进入/退出点保存到每个节点，以用作未来比赛的校准。

###设置*EnterAt*值
![Tuning Graph](img/Tuning%20Graph-10.svg)

* 低于所有门道口的峰值
* 当飞机不靠近门时高于任何峰值
* 高于 *ExitAt*

###设置*ExitAt*值
![Tuning Graph](img/Tuning%20Graph-11.svg)

* 在穿越大门期间出现的任何低于谷值​​
* 高于任何一圈中看到的最低值
* 低于*EnterAt*

ExitAt 值设置为接近 EnterAt，允许计时器快速宣布并显示圈数，但可能无法阻止在单次通过门期间记录多圈数。

### 调优示例
![Tuning Graph](img/Tuning%20Graph-01.svg)<br />
记录两圈。信号两次上升到EnterAt以上，然后下降到ExitAt以下，每个峰值一次。在这两个交叉窗口内，计时器找到最强的信号（噪声过滤后）用作记录的单圈时间。

### 替代调整方法

*Capture*按钮可用于将当前 RSSI 读数存储为每个节点的EnterAt或ExitAt值。也可以手动输入和调整这些值。

在正确的通道上启动四核并使其非常接近计时器几秒钟。这将允许计时器捕获该节点的峰值 RSSI 值。应该对正在调整的每个节点/通道执行此操作。将显示峰值。

#### EnterAt
EnterAt值比较合适开始取值是距计时器约 1.5–3米（5–10 英尺）的位置来获取信号值。

#### ExitAt
ExitAt值比较合适开始取值是距计时器约 6-9米（20-30 英尺）的位置来获取信号值。

## 提示
* 较低的ExitAt值仍然可以提供准确的计时，但系统在宣布圈数之前将等待更长的时间。延迟宣布不会影响计时器的准确性。
* *Minimum Lap Time* 设置可用于防止额外通过，但可能会掩盖过早触发的穿越。强烈建议将行为设置为“Highlight”（而不是“Discard”）并检查每个事件。
* 如果您在比赛期间遇到计时问题，但 RSSI 正在响应飞行员的发射器，请不要停止比赛。比赛完成后保存并访问Marshal页面。 RSSI 历史记录被保存，并且可以使用更新的调整值准确地重新计算比赛。

## 故障排除

### 缺少圈数(节点通常*Clear*)
![Tuning Graph](img/Tuning%20Graph-04.svg)<br />
如果 RSSI 未达到EnterAt，则不会记录圈数。
* 降低 *EnterAt* 值

### 缺少圈数(节点通常*Crossing*)
![Tuning Graph](img/Tuning%20Graph-05.svg)<br />
如果ExitAt太低，则圈数将合并在一起，因为前一圈穿越永远不会完成。
* 提高 *ExitAt*

### 被额外多记圈数
![Tuning Graph](img/Tuning%20Graph-03.svg)<br />
_当EnterAt太低时会发生多记录圏数的情况。_
* 提高 *EnterAt* 直到 *crossings* 仅在计时门边上有效. (保存比赛后使用Marshal页面来确定并保存最佳值。)

### 一次记录多圈
![Tuning Graph](img/Tuning%20Graph-02.svg)<br />
_当ExitAt太接近EnterAt时，会出现过多圈数，因为圈数退出太快。_
* 如果可能的话，提高 *EnterAt* 值
* 降低 *ExitAt* 值

*Minimum Lap Time* 设置始终保留第一个通过，并丢弃出现过早的后续圈数。在许多情况下，第二次穿越实际上是正确的，例如当飞行员错过登机门并返回以完成登机门时。建议将*Minimum Lap Time*行为保留为*Highlight*而不是*Discard*，以便比赛组织者可以手动审查每个案例。

### 圈数需要很长时间才能注册
![Tuning Graph](img/Tuning%20Graph-09.svg)<br />
_如果ExitAt较低，则圈数记录需要很长时间才能完成。这不会影响记录时间的准确性。_
* 提高 *ExitAt*

### 节点永远不会显示*crossing*
![Tuning Graph](img/Tuning%20Graph-07.svg)<br />
_如果 RSSI 从未达到EnterAt ，则不会记录圈数。_
* 降低 *EnterAt*

### 节点永远不会显示*clear*
![Tuning Graph](img/Tuning%20Graph-08.svg)<br />
_如果 RSSI 从未低于ExitAt ，则圈数将不会完成。_
* 提高 *ExitAt*
