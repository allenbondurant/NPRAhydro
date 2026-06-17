# =============================================================================
# HADS Hydromet Station Viewer — Shiny App
# =============================================================================
# Required packages:
#   install.packages(c("shiny", "bslib", "dplyr", "tidyr", "lubridate",
#                      "plotly", "readr", "glue"))
#
# Run with:
#   shiny::runApp("app.R")
#   (or open in RStudio → Session → Set Working Directory → To Source File
#    Location, then click Run App)
#
# Expects a folder called "hydromet_data/" next to this file containing
# per-station CSVs named IKPA2.csv, NUIA2.csv, etc.
# =============================================================================

library(shiny)
library(bslib)
library(dplyr)
library(tidyr)
library(lubridate)
library(plotly)
library(readr)
library(glue)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DATA_DIR <- file.path(getwd(), "hydromet_data")

# PE code metadata: label, imperial unit, metric unit, conversion function
PE_META <- list(
  HG  = list(label = "HG",              imp = "ft",   met = "m",   conv = function(x) x * 0.3048),
  HG2 = list(label = "HG2",             imp = "ft",   met = "m",   conv = function(x) x * 0.3048),
  TW  = list(label = "Water Temp (TW)",  imp = "°F",   met = "°C",  conv = function(x) (x - 32) * 5/9),
  TW2 = list(label = "Water Temp (TW2)", imp = "°F",   met = "°C",  conv = function(x) (x - 32) * 5/9),
  TA  = list(label = "Air Temp",         imp = "°F",   met = "°C",  conv = function(x) (x - 32) * 5/9),
  PC  = list(label = "Precip Accum",     imp = "in",   met = "mm",  conv = function(x) x * 25.4),
  PP  = list(label = "Precip Incr",      imp = "in",   met = "mm",  conv = function(x) x * 25.4),
  US  = list(label = "Wind Speed",       imp = "mph",  met = "m/s", conv = function(x) x * 0.44704),
  UD  = list(label = "Wind Dir",         imp = "°",    met = "°",   conv = function(x) x),
  VB  = list(label = "Battery",          imp = "V",    met = "V",   conv = function(x) x),
  SD  = list(label = "Snow Depth",       imp = "in",   met = "cm",  conv = function(x) x * 2.54),
  SW  = list(label = "Snow Water Equiv", imp = "in",   met = "mm",  conv = function(x) x * 25.4),
  RH  = list(label = "Rel Humidity",     imp = "%",    met = "%",   conv = function(x) x)
)

pe_label <- function(code) {
  sapply(code, function(c) if (c %in% names(PE_META)) PE_META[[c]]$label else c,
         USE.NAMES = FALSE)
}
pe_unit <- function(code, metric) {
  sapply(code, function(c) {
    if (!c %in% names(PE_META)) return("")
    if (metric) PE_META[[c]]$met else PE_META[[c]]$imp
  }, USE.NAMES = FALSE)
}
pe_convert <- function(code, x, metric) {
  if (!code %in% names(PE_META) || !metric) return(x)
  PE_META[[code]]$conv(x)
}

# ---------------------------------------------------------------------------
# Data helpers
# ---------------------------------------------------------------------------

load_station <- function(station_id) {
  path <- file.path(DATA_DIR, paste0(station_id, ".csv"))
  if (!file.exists(path)) return(NULL)
  read_csv(path, show_col_types = FALSE) |>
    mutate(datetime_utc = parse_date_time(datetime_utc,
                                          orders = c("Ymd HM", "Ymd HMS", "Ymd"),
                                          tz = "UTC")) |>
    arrange(datetime_utc)
}

available_stations <- function() {
  csvs <- list.files(DATA_DIR, pattern = "\\.csv$", full.names = FALSE)
  tools::file_path_sans_ext(csvs)
}

pe_columns <- function(df) setdiff(names(df), c("station", "datetime_utc"))

has_data_cols <- function(df) {
  codes <- pe_columns(df)
  codes[sapply(codes, function(c) any(!is.na(df[[c]]) & df[[c]] != ""))]
}

# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------

ui <- page_sidebar(
  title = "Hydromet Station Viewer",
  theme = bs_theme(
    bootswatch   = "flatly",
    primary      = "#2C7BB6",
    bg           = "#F7F9FB",
    fg           = "#1a1a2e",
    base_font    = font_google("Inter"),
    heading_font = font_google("Inter", wght = "600")
  ),

  # ── Sidebar ───────────────────────────────────────────────────────────────
  sidebar = sidebar(
    width = 240,

    h6("Station", class = "text-uppercase text-muted mb-1 mt-2"),
    uiOutput("station_selector"),

    hr(),

    h6("Time Period", class = "text-uppercase text-muted mb-1"),
    dateRangeInput("date_range", NULL,
                   start = Sys.Date() - 7,
                   end   = Sys.Date(),
                   max   = Sys.Date()),
    div(class = "d-flex gap-2 mb-2",
        actionLink("range_1d",  "1 day"),
        actionLink("range_7d",  "7 days"),
        actionLink("range_30d", "30 days")),

    hr(),

    h6("Units", class = "text-uppercase text-muted mb-1"),
    radioButtons("units", NULL,
                 choices  = c("Imperial" = "imperial", "Metric" = "metric"),
                 selected = "imperial",
                 inline   = TRUE),

    hr(),

    actionButton("refresh", "↻  Refresh Data",
                 class = "btn-outline-primary btn-sm w-100")
  ),

  # ── Main panel ────────────────────────────────────────────────────────────
  layout_columns(
    col_widths = 12,

    # Status bar
    uiOutput("status_bar"),

    # Variable checkboxes above chart
    card(
      card_header("Variables"),
      uiOutput("variable_selector")
    ),

    # Chart
    card(
      full_screen = TRUE,
      card_header("Observations"),
      plotlyOutput("main_plot", height = "480px")
    ),

    # Data table
    card(
      card_header("Data Table"),
      tableOutput("data_table")
    )
  )
)

# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------

server <- function(input, output, session) {

  # -- Station list ----------------------------------------------------------
  stations <- reactive({
    input$refresh
    available_stations()
  })

  output$station_selector <- renderUI({
    stas <- stations()
    if (length(stas) == 0) return(p("No CSV files found in hydromet_data/"))
    radioButtons("station", NULL, choices = stas, selected = stas[1])
  })

  # -- Load data -------------------------------------------------------------
  station_data <- reactive({
    req(input$station)
    load_station(input$station)
  })

  # -- Variable checkboxes (horizontal, above chart) -------------------------
  output$variable_selector <- renderUI({
    df <- station_data()
    req(df)
    codes  <- has_data_cols(df)
    metric <- input$units == "metric"
    # Label: "HG (ft)" or "HG (m)" etc.
    choice_names  <- unname(sapply(codes, function(c)
      paste0(pe_label(c), " (", pe_unit(c, metric), ")")))
    choice_values <- unname(codes)
    checkboxGroupInput("variables", NULL,
                       choiceNames  = choice_names,
                       choiceValues = choice_values,
                       selected     = choice_values[1:min(2, length(choice_values))],
                       inline       = TRUE)
  })

  # -- Quick date links ------------------------------------------------------
  observeEvent(input$range_1d,  updateDateRangeInput(session, "date_range",
    start = Sys.Date() - 1,  end = Sys.Date()))
  observeEvent(input$range_7d,  updateDateRangeInput(session, "date_range",
    start = Sys.Date() - 7,  end = Sys.Date()))
  observeEvent(input$range_30d, updateDateRangeInput(session, "date_range",
    start = Sys.Date() - 30, end = Sys.Date()))

  # -- Filtered + converted data ---------------------------------------------
  filtered <- reactive({
    df     <- station_data()
    req(df, input$date_range, input$variables)
    metric <- input$units == "metric"
    start  <- as.POSIXct(input$date_range[1], tz = "UTC")
    end    <- as.POSIXct(input$date_range[2] + 1, tz = "UTC")

    df |>
      filter(datetime_utc >= start, datetime_utc <= end) |>
      select(datetime_utc, all_of(input$variables)) |>
      pivot_longer(-datetime_utc, names_to = "variable", values_to = "value") |>
      mutate(
        value = suppressWarnings(as.numeric(value)),
        value = as.numeric(mapply(pe_convert, variable, value, metric,
                                  SIMPLIFY = TRUE, USE.NAMES = FALSE)),
        label = paste0(pe_label(variable), " (", pe_unit(variable, metric), ")")
      ) |>
      filter(!is.na(value))
  })

  # -- Status bar ------------------------------------------------------------
  output$status_bar <- renderUI({
    df <- station_data()
    req(df)
    n      <- nrow(df)
    latest <- format(max(df$datetime_utc, na.rm = TRUE), "%Y-%m-%d %H:%M UTC")
    div(class = "d-flex gap-3 mb-1",
        span(class = "badge bg-primary", input$station),
        span(class = "text-muted small",
             glue("{n} total rows · latest: {latest}")))
  })

  # -- Plot ------------------------------------------------------------------
  output$main_plot <- renderPlotly({
    df <- filtered()
    req(nrow(df) > 0)

    p <- plot_ly()
    for (v in unique(df$variable)) {
      sub  <- filter(df, variable == v)
      lbl  <- unique(sub$label)
      p <- add_trace(p,
        data = sub,
        x    = ~datetime_utc,
        y    = ~value,
        name = lbl,
        type = "scatter",
        mode = "lines+markers",
        marker = list(size = 4),
        hovertemplate = paste0("<b>", lbl, "</b><br>",
                               "%{x|%Y-%m-%d %H:%M}<br>",
                               "Value: %{y:.2f}<extra></extra>")
      )
    }

    p |> layout(
      xaxis  = list(title = "Date / Time (UTC)", showgrid = TRUE,
                    gridcolor = "#e5e7eb"),
      yaxis  = list(title = "Value",             showgrid = TRUE,
                    gridcolor = "#e5e7eb"),
      legend = list(orientation = "h", y = -0.2),
      paper_bgcolor = "rgba(0,0,0,0)",
      plot_bgcolor  = "rgba(0,0,0,0)",
      hovermode     = "x unified",
      margin        = list(t = 20)
    )
  })

  # -- Data table ------------------------------------------------------------
  output$data_table <- renderTable({
    df <- filtered()
    req(nrow(df) > 0)
    df |>
      mutate(datetime_utc = format(datetime_utc, "%Y-%m-%d %H:%M")) |>
      pivot_wider(names_from = label, values_from = value) |>
      rename("Date / Time (UTC)" = datetime_utc) |>
      arrange(desc(`Date / Time (UTC)`)) |>
      head(200)
  }, striped = TRUE, hover = TRUE, bordered = FALSE, spacing = "s")
}

# ---------------------------------------------------------------------------
shinyApp(ui, server)
