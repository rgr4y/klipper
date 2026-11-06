// Raw byte passthrough on USART1 (PA10=RX, PA9=TX) for stm32g0
//
// The host writes bytes with uart_bridge_write; received bytes come
// back in uart_bridge_rx responses. Flow control is host side: the host
// paces writes to the configured baud so the tx buffer never fills.
// Overflow drops bytes and sets a flag (bit0 rx, bit1 tx) in the next
// uart_bridge_rx; it never shuts the MCU down mid-print.
//
// Copyright (C) 2026  Rob Vella
//
// This file may be distributed under the terms of the GNU GPLv3 license.

#include "basecmd.h" // oid_alloc
#include "board/armcm_boot.h" // armcm_enable_irq
#include "board/misc.h" // DIV_ROUND_CLOSEST
#include "command.h" // DECL_COMMAND
#include "internal.h" // enable_pclock
#include "sched.h" // DECL_TASK

#define CR1_FLAGS (USART_CR1_UE | USART_CR1_RE | USART_CR1_TE   \
                   | USART_CR1_RXNEIE_RXFNEIE)

#define RX_SIZE 128
#define TX_SIZE 1024

struct uart_bridge {
    struct task_wake wake;
    uint8_t oid;
    volatile uint8_t rx_overflow, tx_overflow;
    volatile uint16_t rx_head, rx_tail, tx_head, tx_tail;
    uint8_t rx_buf[RX_SIZE], tx_buf[TX_SIZE];
};

static struct uart_bridge *bridge;

void
uart_bridge_irq(void)
{
    struct uart_bridge *ub = bridge;
    uint32_t isr = USART1->ISR;
    if (isr & USART_ISR_RXNE_RXFNE) {
        uint8_t data = USART1->RDR;
        uint16_t next = (ub->rx_head + 1) % RX_SIZE;
        if (next == ub->rx_tail) {
            ub->rx_overflow = 1;
        } else {
            ub->rx_buf[ub->rx_head] = data;
            ub->rx_head = next;
        }
        sched_wake_task(&ub->wake);
    }
    if (isr & USART_ISR_TXE_TXFNF && USART1->CR1 & USART_CR1_TXEIE_TXFNFIE) {
        if (ub->tx_tail == ub->tx_head) {
            USART1->CR1 = CR1_FLAGS;
        } else {
            USART1->TDR = ub->tx_buf[ub->tx_tail];
            ub->tx_tail = (ub->tx_tail + 1) % TX_SIZE;
        }
    }
}

void
command_config_uart_bridge(uint32_t *args)
{
    if (bridge)
        shutdown("Only one uart_bridge supported");
    struct uart_bridge *ub = oid_alloc(args[0], command_config_uart_bridge
                                       , sizeof(*ub));
    ub->oid = args[0];
    bridge = ub;

    enable_pclock((uint32_t)USART1);
    uint32_t pclk = get_pclock_frequency((uint32_t)USART1);
    USART1->BRR = DIV_ROUND_CLOSEST(pclk, args[1]); // 16x oversampling
    USART1->CR3 = USART_CR3_OVRDIS;
    USART1->CR1 = CR1_FLAGS;
    // Below the host link (0), above the step timer (2)
    armcm_enable_irq(uart_bridge_irq, USART1_IRQn, 1);
    gpio_peripheral(GPIO('A', 10), GPIO_FUNCTION(1), 1);
    gpio_peripheral(GPIO('A', 9), GPIO_FUNCTION(1), 0);
}
DECL_COMMAND(command_config_uart_bridge, "config_uart_bridge oid=%c baud=%u");

void
command_uart_bridge_write(uint32_t *args)
{
    struct uart_bridge *ub = oid_lookup(args[0], command_config_uart_bridge);
    uint8_t len = args[1], *data = command_decode_ptr(args[2]);
    while (len--) {
        uint16_t next = (ub->tx_head + 1) % TX_SIZE;
        if (next == ub->tx_tail) {
            // Never shut down over display traffic: drop and report
            ub->tx_overflow = 1;
            sched_wake_task(&ub->wake);
            break;
        }
        ub->tx_buf[ub->tx_head] = *data++;
        ub->tx_head = next;
    }
    // No lock: the irq clears TXEIE only on an empty buffer. If it fires
    // before this write it either sends the new bytes or (already drained)
    // clears TXEIE, which this write re-enables; either way nothing sticks.
    USART1->CR1 = CR1_FLAGS | USART_CR1_TXEIE_TXFNFIE;
}
DECL_COMMAND_FLAGS(command_uart_bridge_write, HF_IN_SHUTDOWN,
                   "uart_bridge_write oid=%c data=%*s");

void
uart_bridge_task(void)
{
    struct uart_bridge *ub = bridge;
    if (!ub || !sched_check_wake(&ub->wake))
        return;
    uint8_t buf[48], n = 0;
    uint16_t tail = ub->rx_tail;
    while (tail != ub->rx_head && n < sizeof(buf)) {
        buf[n++] = ub->rx_buf[tail];
        tail = (tail + 1) % RX_SIZE;
    }
    ub->rx_tail = tail;
    if (tail != ub->rx_head)
        sched_wake_task(&ub->wake);
    uint8_t overflow = ub->rx_overflow | (ub->tx_overflow << 1);
    ub->rx_overflow = ub->tx_overflow = 0;
    sendf("uart_bridge_rx oid=%c overflow=%c data=%*s"
          , ub->oid, overflow, n, buf);
}
DECL_TASK(uart_bridge_task);
