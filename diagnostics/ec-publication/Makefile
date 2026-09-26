# SPDX-License-Identifier: GPL-2.0-only
obj-m += dgx_ec_publication.o
KDIR ?= /lib/modules/$(shell uname -r)/build

all:
	$(MAKE) -C $(KDIR) M=$(CURDIR) W=1 modules

clean:
	$(MAKE) -C $(KDIR) M=$(CURDIR) clean
