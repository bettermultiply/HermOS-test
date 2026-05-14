CC      = gcc
CFLAGS  = -O2 -Wall -Wextra -std=c11 -static
SRCS    = motivation-daemon.c workloads.c
TARGET  = bench-daemon

.PHONY: all clean

all: $(TARGET)

$(TARGET): $(SRCS) workloads.h
	$(CC) $(CFLAGS) -o $@ $(SRCS)

clean:
	rm -f $(TARGET)