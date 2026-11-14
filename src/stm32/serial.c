// STM32 serial
//
// Copyright (C) 2019  Kevin O'Connor <kevin@koconnor.net>
//
// This file may be distributed under the terms of the GNU GPLv3 license.

#include "autoconf.h" // CONFIG_SERIAL_BAUD
#include "board/armcm_boot.h" // armcm_enable_irq
#include "board/misc.h" // timer_read_time
#include "board/serial_irq.h" // serial_rx_byte
#include "command.h" // DECL_CONSTANT_STR
#include "internal.h" // enable_pclock
#include "sched.h" // DECL_INIT

// Select the configured serial port
#if CONFIG_STM32_SERIAL_USART1
  DECL_CONSTANT_STR("RESERVE_PINS_serial", "PA10,PA9");
  #define GPIO_Rx GPIO('A', 10)
  #define GPIO_Tx GPIO('A', 9)
  #define GPIO_AF_MODE 7
  #define USARTx USART1
  #define USARTx_IRQn USART1_IRQn
#elif CONFIG_STM32_SERIAL_USART1_ALT_PB7_PB6
  DECL_CONSTANT_STR("RESERVE_PINS_serial", "PB7,PB6");
  #define GPIO_Rx GPIO('B', 7)
  #define GPIO_Tx GPIO('B', 6)
  #define GPIO_AF_MODE 7
  #define USARTx USART1
  #define USARTx_IRQn USART1_IRQn
#elif CONFIG_STM32_SERIAL_USART2
  DECL_CONSTANT_STR("RESERVE_PINS_serial", "PA3,PA2");
  #define GPIO_Rx GPIO('A', 3)
  #define GPIO_Tx GPIO('A', 2)
  #define GPIO_AF_MODE 7
  #define USARTx USART2
  #define USARTx_IRQn USART2_IRQn
#elif CONFIG_STM32_SERIAL_USART2_ALT_PD6_PD5
  DECL_CONSTANT_STR("RESERVE_PINS_serial", "PD6,PD5");
  #define GPIO_Rx GPIO('D', 6)
  #define GPIO_Tx GPIO('D', 5)
  #define GPIO_AF_MODE 7
  #define USARTx USART2
  #define USARTx_IRQn USART2_IRQn
#elif CONFIG_STM32_SERIAL_USART3
  DECL_CONSTANT_STR("RESERVE_PINS_serial", "PB11,PB10");
  #define GPIO_Rx GPIO('B', 11)
  #define GPIO_Tx GPIO('B', 10)
  #define GPIO_AF_MODE 7
  #define USARTx USART3
  #define USARTx_IRQn USART3_IRQn
#elif CONFIG_STM32_SERIAL_USART3_ALT_PD9_PD8
  DECL_CONSTANT_STR("RESERVE_PINS_serial", "PD9,PD8");
  #define GPIO_Rx GPIO('D', 9)
  #define GPIO_Tx GPIO('D', 8)
  #define GPIO_AF_MODE 7
  #define USARTx USART3
  #define USARTx_IRQn USART3_IRQn
#elif CONFIG_STM32_SERIAL_USART6
  DECL_CONSTANT_STR("RESERVE_PINS_serial", "PA12,PA11");
  #define GPIO_Rx GPIO('A', 12)
  #define GPIO_Tx GPIO('A', 11)
  #define GPIO_AF_MODE 8
  #define USARTx USART6
  #define USARTx_IRQn USART6_IRQn
#elif CONFIG_STM32_SERIAL_USART6_ALT_PC7_PC6
  DECL_CONSTANT_STR("RESERVE_PINS_serial", "PC7,PC6");
  #define GPIO_Rx GPIO('C', 7)
  #define GPIO_Tx GPIO('C', 6)
  #define GPIO_AF_MODE 8
  #define USARTx USART6
  #define USARTx_IRQn USART6_IRQn
#endif

#define CR1_FLAGS (USART_CR1_UE | USART_CR1_RE | USART_CR1_TE   \
                   | USART_CR1_RXNEIE)

void
USARTx_IRQHandler(void)
{
    uint32_t sr = USARTx->SR;
    if (sr & (USART_SR_RXNE | USART_SR_ORE)) {
        // The ORE flag is automatically cleared by reading SR, followed
        // by reading DR.
        serial_rx_byte(USARTx->DR);
    }
    if (sr & USART_SR_TXE && USARTx->CR1 & USART_CR1_TXEIE) {
        uint8_t data;
        int ret = serial_get_tx_byte(&data);
        if (ret)
            USARTx->CR1 = CR1_FLAGS;
        else
            USARTx->DR = data;
    }
}

void
serial_enable_tx_irq(void)
{
    USARTx->CR1 = CR1_FLAGS | USART_CR1_TXEIE;
}

static void
serial_set_baud(uint32_t baud)
{
    uint32_t pclk = get_pclock_frequency((uint32_t)USARTx);
    uint32_t div = DIV_ROUND_CLOSEST(pclk, baud);
    USARTx->BRR = (((div / 16) << USART_BRR_DIV_Mantissa_Pos)
                   | ((div % 16) << USART_BRR_DIV_Fraction_Pos));
}

// Until a valid message arrives, alternate between the configured and
// the fallback baud rate so that a host using either rate can connect.
#define FALLBACK_PERIOD_MS 20000

void
serial_fallback_task(void)
{
    static uint8_t done, use_fallback;
    static uint32_t last, elapsed_ms, ticks;
    if (!CONFIG_SERIAL_BAUD_FALLBACK || done)
        return;
    if (serial_rx_valid) {
        done = 1;
        return;
    }
    uint32_t now = timer_read_time();
    if (!last)
        last = now;
    ticks += now - last;
    last = now;
    uint32_t ms_ticks = timer_from_us(1000);
    while (ticks >= ms_ticks) {
        ticks -= ms_ticks;
        elapsed_ms++;
    }
    if (elapsed_ms >= FALLBACK_PERIOD_MS) {
        elapsed_ms = 0;
        use_fallback = !use_fallback;
        serial_set_baud(use_fallback ? CONFIG_SERIAL_BAUD_FALLBACK
                        : CONFIG_SERIAL_BAUD);
    }
}
DECL_TASK(serial_fallback_task);

void
serial_init(void)
{
    enable_pclock((uint32_t)USARTx);

    serial_set_baud(CONFIG_SERIAL_BAUD);
    USARTx->CR1 = CR1_FLAGS;
    armcm_enable_irq(USARTx_IRQHandler, USARTx_IRQn, 0);

    gpio_peripheral(GPIO_Rx, GPIO_FUNCTION(GPIO_AF_MODE), 1);
    gpio_peripheral(GPIO_Tx, GPIO_FUNCTION(GPIO_AF_MODE), 0);
}
DECL_INIT(serial_init);
