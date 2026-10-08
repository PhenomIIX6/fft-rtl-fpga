module de2_115 #(
    parameter CLK_IN_FREQ   = 50_000_000,  // DE2-115 onboard oscillator (CLOCK_50)
    parameter CLK_FREQ      = 50_000_000,  // Must be configured using pll!

    // ================= UART =================
    parameter UART_BAUD       = 115_200,   // line baud rate
    parameter UART_OVERSAMPLE = 8,         // UART RX/TX oversampling factor

    // ================= FFT =================
    parameter N               = 512,       // FFT length (power of two)
    parameter WIDTH           = 16,        // sample width, per I/Q component [bits]
    parameter INTEGER_WIDTH   = 2,         // integer bits per component
    parameter ROUND           = 1,         // round (1) vs truncate (0) after scaling
    parameter RADIX_OUT_SCALE = 1,         // 1/2 output scaling per radix-2 stage
    parameter SATURATION      = 1,         // saturate (1) vs wrap (0) on overflow

    // ================= Derived / miscellaneous =================
    parameter SAMPLE_WIDTH  = 2*WIDTH,                                // I+Q combined width
    parameter UART_PRESCALE = CLK_FREQ / (UART_BAUD * UART_OVERSAMPLE),
    parameter DEBOUNCE_BITS = 20                                      // reset release counter width

) (
    input  logic        CLOCK_50,
    input  logic [3:0]  KEY, // active-low (KEY[0] = reset_n)
    inout  wire  [35:0] GPIO // GPIO_0 header: [0] = RXD, [1] = TXD
);

    logic sys_clk;

    clock_pll pll_inst (
        .clk_in (CLOCK_50),
        .areset (~KEY[0]),
        .clk_out(sys_clk),
        .locked()
    );

    logic rxd;
    logic txd;

    assign rxd     = GPIO[0];   // external TX -> FPGA RXD
    assign GPIO[1] = txd;       // FPGA TXD   -> external RX

   top #(
        .FCLK            (CLK_FREQ),
        .UART_BAUD       (UART_BAUD),
        .UART_OVERSAMPLE (UART_OVERSAMPLE),
        .N               (N),
        .WIDTH           (WIDTH),
        .INTEGER_WIDTH   (INTEGER_WIDTH),
        .ROUND           (ROUND),
        .RADIX_OUT_SCALE (RADIX_OUT_SCALE),
        .SATURATION      (SATURATION),
        .SAMPLE_WIDTH    (SAMPLE_WIDTH),
        .UART_PRESCALE   (UART_PRESCALE)
    ) top_inst (
        .clk    (sys_clk),
        .arst_n (arst_n ),
        .txd    (txd    ),
        .rxd    (rxd    )
    );

endmodule
