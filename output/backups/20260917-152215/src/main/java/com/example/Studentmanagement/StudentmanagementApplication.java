package com.example.Studentmanagement;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;

@SpringBootApplication
public class StudentmanagementApplication {

	private static final Logger logger = LoggerFactory.getLogger(StudentmanagementApplication.class);

	public static void main(String[] args) {
		logger.info("Starting Student Management application");
		System.out.println("Starting Student Management application");
		SpringApplication.run(StudentmanagementApplication.class, args);
	}

}
