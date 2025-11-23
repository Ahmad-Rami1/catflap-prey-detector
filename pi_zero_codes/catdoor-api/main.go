package main

import (
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http"
	"os"
	"strings"
	"time"
)

const controllerAddr = "127.0.0.1:8765"
const configPath = "/home/rami/catdoor-config.json"

// Config represents the catdoor configuration
type Config struct {
	LastDetected string `json:"last_detected"`
	LockedUntil  string `json:"locked_until,omitempty"`
}

// sendToController connects to the Python TCP controller and sends a command.
func sendToController(cmd string) (string, error) {
	conn, err := net.DialTimeout("tcp", controllerAddr, 2*time.Second)
	if err != nil {
		return "", fmt.Errorf("cannot connect to controller: %w", err)
	}
	defer conn.Close()

	_, err = io.WriteString(conn, cmd+"\n")
	if err != nil {
		return "", fmt.Errorf("failed to send command: %w", err)
	}

	_ = conn.SetReadDeadline(time.Now().Add(2 * time.Second))
	resp, err := io.ReadAll(conn)
	if err != nil {
		return "", fmt.Errorf("failed to read response: %w", err)
	}

	return string(resp), nil
}

// loadConfig reads the config file
func loadConfig() (*Config, error) {
	data, err := os.ReadFile(configPath)
	if err != nil {
		if os.IsNotExist(err) {
			return &Config{}, nil
		}
		return nil, err
	}

	var config Config
	if err := json.Unmarshal(data, &config); err != nil {
		return nil, err
	}
	return &config, nil
}

// saveConfig writes the config file
func saveConfig(config *Config) error {
	data, err := json.MarshalIndent(config, "", "  ")
	if err != nil {
		return err
	}
	return os.WriteFile(configPath, data, 0644)
}

// detectedHandler handles prey detection events
func detectedHandler(w http.ResponseWriter, r *http.Request) {
	fmt.Println("🚨 Prey detected! Locking catflap...")

	// Set mode to RED immediately
	resp, err := sendToController("RED")
	if err != nil {
		http.Error(w, "failed to lock catflap: "+err.Error(), http.StatusBadGateway)
		return
	}

	// Update config with detection timestamp
	now := time.Now()
	unlockTime := now.Add(5 * time.Minute)

	config := &Config{
		LastDetected: now.Format(time.RFC3339),
		LockedUntil:  unlockTime.Format(time.RFC3339),
	}

	if err := saveConfig(config); err != nil {
		fmt.Printf("Warning: failed to save config: %v\n", err)
	}

	fmt.Printf("✅ Catflap locked until %s\n", unlockTime.Format("15:04:05"))

	// Start goroutine to auto-unlock after 5 minutes
	go func() {
		time.Sleep(5 * time.Minute)
		fmt.Println("⏰ Auto-unlocking catflap after 5 minutes...")

		unlockResp, err := sendToController("GREEN")
		if err != nil {
			fmt.Printf("❌ Failed to auto-unlock: %v\n", err)
			return
		}

		fmt.Printf("✅ Auto-unlock complete: %s\n", unlockResp)

		// Clear locked_until in config
		config, err := loadConfig()
		if err == nil {
			config.LockedUntil = ""
			saveConfig(config)
		}
	}()

	// Return success response
	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(map[string]interface{}{
		"status":       "locked",
		"locked_until": unlockTime.Format(time.RFC3339),
		"controller":   strings.TrimSpace(resp),
	})
}

// modeHandler handles requests like /mode/green, /mode/yellow, /mode/red
func modeHandler(w http.ResponseWriter, r *http.Request) {
	parts := strings.Split(strings.Trim(r.URL.Path, "/"), "/")
	if len(parts) != 2 || parts[0] != "mode" {
		http.NotFound(w, r)
		return
	}

	name := strings.ToUpper(parts[1])
	switch name {
	case "GREEN", "YELLOW", "RED":
		resp, err := sendToController(name)
		if err != nil {
			http.Error(w, "controller error: "+err.Error(), http.StatusBadGateway)
			return
		}
		w.Header().Set("Content-Type", "text/plain; charset=utf-8")
		fmt.Fprint(w, resp)
	default:
		http.Error(w, "unknown mode (use green|yellow|red)", http.StatusBadRequest)
	}
}

// statusHandler handles /status
func statusHandler(w http.ResponseWriter, r *http.Request) {
	resp, err := sendToController("STATUS")
	if err != nil {
		http.Error(w, "controller error: "+err.Error(), http.StatusBadGateway)
		return
	}
	w.Header().Set("Content-Type", "text/plain; charset=utf-8")
	fmt.Fprint(w, resp)
}

// logsHandler parses and returns the logs as JSON
func logsHandler(w http.ResponseWriter, r *http.Request) {
	logType := r.URL.Query().Get("type")

	var filePath string
	switch strings.ToLower(logType) {
	case "reed":
		filePath = "/home/rami/logs/reed_logs.txt"
	case "radar":
		filePath = "/home/rami/logs/sensor_logs.txt"
	default:
		http.Error(w, "invalid type parameter (use type=reed or type=radar)", http.StatusBadRequest)
		return
	}

	content, err := os.ReadFile(filePath)
	if err != nil {
		http.Error(w, "failed to read log file: "+err.Error(), http.StatusInternalServerError)
		return
	}

	lines := strings.Split(string(content), "\n")
	var logs []map[string]string

	for _, line := range lines {
		line = strings.TrimSpace(line)
		if line == "" {
			continue
		}

		var timestamp, message string

		if logType == "reed" {
			parts := strings.SplitN(line, " ", 3)
			if len(parts) >= 3 {
				timestamp = parts[0] + " " + parts[1]
				message = parts[2]
			}
		} else if logType == "radar" {
			if strings.HasPrefix(line, "[") {
				endBracket := strings.Index(line, "]")
				if endBracket > 0 {
					timestamp = line[1:endBracket]
					message = strings.TrimSpace(line[endBracket+1:])
				}
			}
		}

		if timestamp != "" && message != "" {
			logs = append(logs, map[string]string{
				"timestamp": timestamp,
				"message":   message,
			})
		}
	}

	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	json.NewEncoder(w).Encode(logs)
}

func main() {
	http.HandleFunc("/mode/", modeHandler)
	http.HandleFunc("/status", statusHandler)
	http.HandleFunc("/logs", logsHandler)
	http.HandleFunc("/detected", detectedHandler) // NEW ENDPOINT

	addr := ":8080"
	fmt.Println("🚀 REST API listening on", addr)
	fmt.Println("📡 Endpoints:")
	fmt.Println("  - POST/GET /detected (prey detection)")
	fmt.Println("  - GET /mode/{green|yellow|red}")
	fmt.Println("  - GET /status")
	fmt.Println("  - GET /logs?type={reed|radar}")

	if err := http.ListenAndServe(addr, nil); err != nil {
		panic(err)
	}
}
