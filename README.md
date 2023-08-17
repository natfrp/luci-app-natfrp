# SakuraFrp 启动器 OpenWrt 软件包

实验性项目，我们没有采用 OpenWrt 官方的构建系统，因为：

- 构建 IPK 的过程不涉及编译，只是单纯的打了个包
- 暂时没有加入任何 opkg 源的计划

如果您想在自己编译的镜像中包含此软件包，可以考虑直接把 IPK 塞到 rootfs 里然后第一次启动时安装。

也可以挪一下文件，从 [Nyatwork CDN](https://nya.globalslb.net/natfrp/client/launcher-unix/) 获取编译过程中用到的二进制，然后参考这个 Makefile：

```makefile
include $(TOPDIR)/rules.mk

PKG_NAME:=luci-app-natfrp
PKG_VERSION:=3.0.0
PKG_RELEASE:=1
PKG_LICENSE:=AGPL-3.0-only

LUCI_TITLE:=SakuraFrp Service LuCI Interface
LUCI_MAINTAINER:=iDea Leaper

LUCI_URL:=https://github.com/natfrp/luci-app-natfrp
LUCI_DEPENDS:=+luci-compat

include $(INCLUDE_DIR)/package.mk

define Package/$(PKG_NAME)/description
  SakuraFrp 启动器 OpenWRT 软件包
endef

define Package/$(PKG_NAME)/conffiles
/etc/config/natfrp
endef

define Package/$(PKG_NAME)/prerm
#!/bin/sh
rm -rf /etc/natfrp
exit 0
endef

include $(TOPDIR)/feeds/luci/luci.mk

# call BuildPackage - OpenWrt buildroot signature
```

### 下载

获取 IPK 文件：https://nya.globalslb.net/natfrp/client/launcher-openwrt/

只要架构正确就可以直接安装 IPK，应该不存在兼容问题，二进制文件都是全静态的。

### 系统需求

 - OpenWrt 18 及以上版本
 - 安装 `luci-compat` 包

对于更早版本的系统（14 ~ 17）可以参考下列操作：

1. 忽略依赖强制安装：

  ```bash
  opkg install --force-depends ./luci-app-natfrp_<架构>.ipk
  ```

1. 到 LuCI 配置完成后，手动启动服务：

  ```bash
  /etc/init.d/natfrp start
  ```

1. 检查日志输出：

  ```bash
  logread |grep natfrp
  ```

如果您还在使用 AA 等版本的上古系统，请自行从 CDN 获取二进制并编写启动脚本和配置文件，不推荐安装此处提供的 IPK。
