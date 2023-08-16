# SakuraFrp 启动器 OpenWRT 软件包

实验性项目，我们没有采用 OpenWRT 官方的构建系统，因为：

- 打包过程完全没有编译动作
- 暂时没有加入任何 opkg 源的计划

### 下载

获取 IPK 文件：https://nya.globalslb.net/natfrp/client/launcher-openwrt/

只要架构正确就可以直接安装 IPK，应该不存在兼容问题，二进制文件都是全静态的。

### 系统需求

 - OpenWRT 18 及以上版本
 - 安装 `luci-compat` 包

对于更早版本的 LEDE 系统，可以忽略 `luci-compat` 依赖强制安装，然后使用 `/etc/init.d/natfrp start` 手动启动服务。但是在 LuCI 中可能无法正确显示状态。
